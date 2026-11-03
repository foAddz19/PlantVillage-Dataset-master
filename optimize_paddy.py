"""Resume the small rice model under a CPU cap; select only on validation INT8."""
from __future__ import annotations

import argparse
from collections import Counter
import ctypes
from datetime import datetime, timezone
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import time


def cpu_budget(threads):
    for name in ('TF_NUM_INTRAOP_THREADS','OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):
        os.environ[name]=str(threads)
    os.environ['TF_NUM_INTEROP_THREADS']='1'
    os.environ['CUDA_VISIBLE_DEVICES']='-1'
    os.environ['TF_CPP_MIN_LOG_LEVEL']='2'
    if os.name=='nt':
        from ctypes import wintypes
        kernel=ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        kernel.SetPriorityClass.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.GetProcessAffinityMask.argtypes=[wintypes.HANDLE,ctypes.POINTER(ctypes.c_size_t),ctypes.POINTER(ctypes.c_size_t)]
        kernel.SetProcessAffinityMask.argtypes=[wintypes.HANDLE,ctypes.c_size_t]
        process=kernel.GetCurrentProcess();allowed=ctypes.c_size_t();system=ctypes.c_size_t()
        if not kernel.SetPriorityClass(process,0x4000):raise OSError('Cannot lower training priority')
        if not kernel.GetProcessAffinityMask(process,ctypes.byref(allowed),ctypes.byref(system)):raise OSError('Cannot inspect CPU affinity')
        bits=[1<<i for i in range(64) if allowed.value&(1<<i)][:threads]
        if len(bits)!=threads or not kernel.SetProcessAffinityMask(process,sum(bits)):raise OSError('Cannot cap training CPU affinity')
    elif hasattr(os,'sched_getaffinity'):
        available=sorted(os.sched_getaffinity(0));os.sched_setaffinity(0,available[:threads]);os.nice(10)


def stratified_calibration(rows, labels, count, seed):
    import random
    if type(count) is not int or count < 1 or not labels or len(labels) != len(set(labels)):
        raise ValueError('Calibration requires a positive count and unique labels')
    seen=set()
    for row in rows:
        if row.get('split')!='train' or row.get('label') not in labels or not isinstance(row.get('path'),str) or row['path'] in seen:
            raise ValueError('Calibration requires unique, labelled training images only')
        seen.add(row['path'])
    rng=random.Random(seed)
    buckets={label:[row for row in rows if row['label']==label] for label in labels}
    for bucket in buckets.values():rng.shuffle(bucket)
    result=[]
    while len(result)<min(count,len(rows)):
        for label in labels:
            if buckets[label]:result.append(buckets[label].pop())
            if len(result)==min(count,len(rows)):break
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-dir',type=Path,required=True)
    parser.add_argument('--dataset-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--resume-keras',type=Path)
    parser.add_argument('--epochs',type=int,default=12)
    parser.add_argument('--unfreeze-layers',type=int,default=60)
    parser.add_argument('--learning-rate',type=float,default=3e-5)
    parser.add_argument('--batch-size',type=int,default=8)
    parser.add_argument('--threads',type=int,default=2)
    parser.add_argument('--calibration-images',type=int,default=512)
    parser.add_argument('--max-memory-mib',type=int,default=1800)
    args=parser.parse_args()
    if min(args.epochs,args.unfreeze_layers,args.batch_size,args.threads,args.calibration_images,args.max_memory_mib)<1 or args.threads>2:
        parser.error('Use positive settings and at most 2 CPU threads for this low-load experiment')
    if not math.isfinite(args.learning_rate) or args.learning_rate<=0:
        parser.error('Learning rate must be finite and positive')
    args.baseline_dir=args.baseline_dir.resolve();args.dataset_dir=args.dataset_dir.resolve();args.output=args.output.resolve()
    for protected in (args.baseline_dir,args.dataset_dir,(Path(__file__).resolve().parent/'models/paddy').resolve()):
        if args.output.is_relative_to(protected) or protected.is_relative_to(args.output):
            parser.error('Output must not overlap baseline, data or active models')
    args.output.mkdir(parents=True,exist_ok=True)
    cpu_budget(args.threads)
    import numpy as np
    import tensorflow as tf
    from PIL import Image
    from leaflab.images import read_image
    from train_paddy import atomic_json,evaluate_probabilities,export_int8
    from benchmark_paddy import memory_mib

    tf.config.threading.set_intra_op_parallelism_threads(args.threads)
    tf.config.threading.set_inter_op_parallelism_threads(1)
    tf.keras.utils.set_random_seed(42)
    base=json.loads((args.baseline_dir/'model.json').read_text(encoding='utf-8'))
    labels=tuple(base['labels']);records=base['manifest'];size=base['input']['shape'][1]
    if base['input']['shape']!=[1,160,160,3]:raise ValueError('This experiment keeps baseline 160x160 cost unchanged')
    subsets={split:[row for row in records if row['split']==split] for split in ('train','validation','test')}
    # Check only train and validation here: test is not read or used for selection.
    for row in subsets['train']+subsets['validation']:
        path=(args.dataset_dir/row['path']).resolve()
        if not path.is_relative_to(args.dataset_dir) or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            raise ValueError('Training/validation source mismatch: '+row['path'])
    model_path=args.resume_keras or args.baseline_dir/Path(base['model_file']).with_suffix('.keras').name
    model=tf.keras.models.load_model(model_path,compile=False)
    if model.count_params()!=base['metadata']['parameter_count']:raise ValueError('Architecture changed')
    nested=[layer for layer in model.layers if isinstance(layer,tf.keras.Model)]
    backbone=max(nested,key=lambda layer:len(layer.layers))
    model.trainable=True
    for index,layer in enumerate(backbone.layers):
        layer.trainable=index>=len(backbone.layers)-args.unfreeze_layers and not isinstance(layer,tf.keras.layers.BatchNormalization)
    model.compile(optimizer=tf.keras.optimizers.Adam(args.learning_rate),loss='sparse_categorical_crossentropy',metrics=['accuracy'])
    augmentation=tf.keras.Sequential([tf.keras.layers.RandomFlip('horizontal'),
        tf.keras.layers.RandomRotation(.06,fill_mode='reflect'),tf.keras.layers.RandomContrast(.12)])

    def pixels(row):
        return np.asarray(read_image(args.dataset_dir/row['path']).resize((size,size),Image.Resampling.BILINEAR),dtype=np.float32)
    def dataset(rows,training=False):
        rng=np.random.default_rng(42)
        def generate():
            order=np.arange(len(rows))
            if training:rng.shuffle(order)
            for index in order:
                row=rows[index];yield pixels(row),labels.index(row['label'])
        ds=tf.data.Dataset.from_generator(generate,output_signature=(tf.TensorSpec((size,size,3),tf.float32),tf.TensorSpec((),tf.int32)))
        options=tf.data.Options();options.threading.private_threadpool_size=1;options.threading.max_intra_op_parallelism=args.threads
        ds=ds.with_options(options).apply(tf.data.experimental.assert_cardinality(len(rows))).batch(args.batch_size)
        if training:ds=ds.map(lambda x,y:(augmentation(x,training=True),y),num_parallel_calls=1)
        return ds.prefetch(1)
    train=dataset(subsets['train'],True);validation=dataset(subsets['validation'])
    started=time.time();history=[]
    initial=model.evaluate(validation,verbose=0,return_dict=True)
    print('Baseline float validation:',json.dumps(initial),flush=True)
    best_accuracy=args.output/'best-accuracy.weights.h5';best_loss=args.output/'best-loss.weights.h5'
    model.save_weights(best_accuracy);model.save_weights(best_loss)

    class Progress(tf.keras.callbacks.Callback):
        def __init__(self):super().__init__();self.last=0;self.epoch=0;self.stop_reason=None
        def on_epoch_begin(self,epoch,logs=None):self.epoch=epoch+1
        def on_train_batch_end(self,batch,logs=None):
            if time.time()-self.last<15:return
            self.last=time.time();memory=memory_mib()
            status=dict(stage='training',epoch=self.epoch,batch=batch+1,epochs=args.epochs,seconds=round(time.time()-started,1),memory_mib=memory)
            atomic_json(args.output/'progress.json',status)
            if memory and memory['current']>args.max_memory_mib:
                self.stop_reason='memory_budget';self.model.stop_training=True
        def on_epoch_end(self,epoch,logs=None):
            logs={key:float(value) for key,value in (logs or {}).items()}
            row=dict(epoch=epoch+1,**logs,seconds=round(time.time()-started,1),memory_mib=memory_mib())
            history.append(row);atomic_json(args.output/'history.json',dict(initial=initial,epochs=history))
            print('PROGRESS',json.dumps(row),flush=True)
    progress=Progress()
    callbacks=[tf.keras.callbacks.ModelCheckpoint(str(best_accuracy),monitor='val_accuracy',mode='max',save_best_only=True,save_weights_only=True,initial_value_threshold=initial['accuracy']),
        tf.keras.callbacks.ModelCheckpoint(str(best_loss),monitor='val_loss',mode='min',save_best_only=True,save_weights_only=True,initial_value_threshold=initial['loss']),
        tf.keras.callbacks.ReduceLROnPlateau(monitor='val_loss',factor=.5,patience=2,min_lr=2e-6,verbose=1),
        tf.keras.callbacks.EarlyStopping(monitor='val_accuracy',mode='max',patience=4,min_delta=.001),progress]
    print('Training cap:',args.threads,'logical CPUs, below-normal priority; batch',args.batch_size,'BN frozen; trainable params',sum(int(np.prod(weight.shape)) for weight in model.trainable_weights),flush=True)
    model.fit(train,validation_data=validation,epochs=args.epochs,callbacks=callbacks,verbose=2,shuffle=False)
    calibration=stratified_calibration(subsets['train'],labels,args.calibration_images,42)
    def representative():
        for row in calibration:yield [pixels(row)[None]]
    results=[]
    for name,checkpoint in (('best-accuracy',best_accuracy),('best-loss',best_loss)):
        model.load_weights(checkpoint)
        probabilities=model.predict(validation,verbose=0)
        float_metrics=evaluate_probabilities(probabilities,[labels.index(row['label']) for row in subsets['validation']],labels)
        output=args.output/name
        metadata={**base['metadata'],'created_at':datetime.now(timezone.utc).isoformat(),
            'accuracy':None,'float_metrics':None,'validation_float_metrics':float_metrics,
            'evaluation_split':'validation','deployment_ready':False,
            'optimization':dict(parent_sha256=base['sha256'],resume_keras=str(model_path),
                epochs_requested=args.epochs,epochs_completed=len(history),unfreeze_layers=args.unfreeze_layers,
                learning_rate=args.learning_rate,batch_size=args.batch_size,threads=args.threads,
                cpu_affinity_logical_cpus=args.threads,calibration_images=len(calibration),
                calibration_class_counts=dict(Counter(row['label'] for row in calibration)),
                checkpoint=name,stop_reason=progress.stop_reason,history=history),
            'seconds':round(time.time()-started,1),'parameter_count':model.count_params()}
        config=export_int8(model,representative,output,metadata,records,
            lambda:(pixels(row) for row in subsets['validation']),[labels.index(row['label']) for row in subsets['validation']],labels)
        model.save(output/Path(config['model_file']).with_suffix('.keras').name)
        item=dict(checkpoint=name,validation_float_accuracy=float_metrics['accuracy'],
            validation_int8_accuracy=config['metadata']['accuracy'],validation_int8_balanced_accuracy=config['metadata']['balanced_accuracy'],
            model_bytes=config['metadata']['model_bytes'],sha256=config['sha256'])
        results.append(item);print('CANDIDATE',json.dumps(item),flush=True);gc.collect()
    atomic_json(args.output/'selection.json',dict(protocol='Choose on validation INT8 only; held-out test not read by training or selection',
        initial=initial,candidates=results,selected=max(results,key=lambda item:item['validation_int8_accuracy']),
        seconds=round(time.time()-started,1),training_memory_mib=memory_mib()))
    atomic_json(args.output/'progress.json',dict(stage='complete',seconds=round(time.time()-started,1),memory_mib=memory_mib()))
    print('COMPLETE validation selection:',args.output/'selection.json',flush=True)


if __name__=='__main__':main()
