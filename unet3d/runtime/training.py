from tqdm import tqdm

import torch
from torch.optim import Adam, SGD
from torch.cuda.amp import autocast, GradScaler

from runtime.distributed_utils import get_rank, reduce_tensor, get_world_size
from runtime.inference import evaluate
from runtime.logging import mllog_event, mllog_start, mllog_end, CONSTANTS
import time
import numba
from utility import perftrace
from mpi4py import MPI
import numpy as np
comm = MPI.COMM_WORLD

def emulate_compute(device, sec):
    if (str(device).find("GPU")!=-1):
        print("Putting GPU into sleep for %10.5f sec"%sec)
        numba.cuda.nanosleep(sec*1000000000)
    else:
        time.sleep(sec)
def get_optimizer(params, flags):
    if flags.optimizer == "adam":
        optim = Adam(params, lr=flags.learning_rate, weight_decay=flags.weight_decay)
    elif flags.optimizer == "sgd":
        optim = SGD(params, lr=flags.learning_rate, momentum=flags.momentum, nesterov=True,
                    weight_decay=flags.weight_decay)
    elif flags.optimizer == "lamb":
        import apex
        optim = apex.optimizers.FusedLAMB(params, lr=flags.learning_rate, betas=flags.lamb_betas,
                                          weight_decay=flags.weight_decay)
    else:
        raise ValueError("Optimizer {} unknown.".format(flags.optimizer))
    return optim


def lr_warmup(optimizer, init_lr, lr, current_epoch, warmup_epochs):
    scale = current_epoch / warmup_epochs
    for param_group in optimizer.param_groups:
        param_group['lr'] = init_lr + (lr - init_lr) * scale

@perftrace.event_logging
def train(flags, model, train_loader, val_loader, loss_fn, score_fn, device, callbacks, is_distributed, sleep=-1):
    rank = get_rank()
    world_size = get_world_size()
    torch.backends.cudnn.benchmark = flags.cudnn_benchmark
    torch.backends.cudnn.deterministic = flags.cudnn_deterministic

    optimizer = get_optimizer(model.parameters(), flags)
    if flags.lr_decay_epochs:
        scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer,
                                                         milestones=flags.lr_decay_epochs,
                                                         gamma=flags.lr_decay_factor)
    scaler = GradScaler()

    model.to(device)
    loss_fn.to(device)
    if is_distributed:
        model = torch.nn.parallel.DistributedDataParallel(model,
                                                          device_ids=[flags.local_rank],
                                                          output_device=flags.local_rank)

    is_successful = False
    diverged = False
    next_eval_at = flags.start_eval_at
    model.train()
    for callback in callbacks:
        callback.on_fit_start()
    for epoch in range(1, flags.epochs + 1):
        cumulative_loss = []
        if epoch <= flags.lr_warmup_epochs and flags.lr_warmup_epochs > 0:
            lr_warmup(optimizer, flags.init_learning_rate, flags.learning_rate, epoch, flags.lr_warmup_epochs)
        mllog_start(key=CONSTANTS.BLOCK_START, sync=False,
                    metadata={CONSTANTS.FIRST_EPOCH_NUM: epoch, CONSTANTS.EPOCH_COUNT: 1})
        mllog_start(key=CONSTANTS.EPOCH_START, metadata={CONSTANTS.EPOCH_NUM: epoch}, sync=False)

        if is_distributed:
            train_loader.sampler.set_epoch(epoch)

        loss_value = None
        total_loss_value = 0
        optimizer.zero_grad()
        t0 = time.time()
        for iteration, batch in enumerate(tqdm(train_loader, disable=(rank != 0) or not flags.verbose)):
            image, label = batch
            t1 = time.time()
            perftrace.event_complete(name=f"loading batch:{image.shape[0]}", cat="train", ts = t0, dur=t1 - t0)
            t0 = time.time()
            image, label = image.to(device), label.to(device)
            t1 = time.time()
            perftrace.event_complete(name=f"H2D", cat="train", ts = t0, dur=t1 - t0)
            t0 = time.time()
            for callback in callbacks:
                callback.on_batch_start()
            if (sleep >= 0):
                emulate_compute(device, sleep)
                t1 = time.time()
                perftrace.event_complete(name=f"emulate_compute:step-{iteration}", cat="train", ts = t0, dur = t1 - t0)
                if (rank==0):
                    print(" training time [%d]: %10.5f" %(iteration, t1 - t0))
                continue
            
            with autocast(enabled=flags.amp):
                output = model(image)
                loss_value = loss_fn(output, label)
                loss_value /= flags.ga_steps

            if flags.amp:
                scaler.scale(loss_value).backward()
            else:
                loss_value.backward()

            if (iteration + 1) % flags.ga_steps == 0:
                if flags.amp:
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    optimizer.step()

                optimizer.zero_grad()
            cumulative_loss.append(loss_value.detach().cpu().numpy())
#            loss_value = reduce_tensor(loss_value, world_size).detach().cpu().numpy()
#            cumulative_loss.append(loss_value)
            t1 = time.time()
            perftrace.event_complete(name=f"compute:step-{iteration}", cat="train", ts = t0, dur = t1 - t0)
            if (rank==0):
                print(" training time [%d]: %10.8f (s)     %10.8f (ms)" %(iteration, t1 - t0, t0*1000))
            t0 = time.time()
        total_loss = sum(cumulative_loss)
        tt = np.zeros(1, dtype=MPI.FLOAT)
        comm.Allreduce(total_loss, tt, op=MPI.SUM)
        mllog_end(key=CONSTANTS.EPOCH_STOP, sync=False,
                  metadata={CONSTANTS.EPOCH_NUM: epoch, 'current_lr': optimizer.param_groups[0]['lr']})
        if flags.lr_decay_epochs:
            scheduler.step()

        if epoch == next_eval_at:
            next_eval_at += flags.evaluate_every
            del output
            mllog_start(key=CONSTANTS.EVAL_START, value=epoch, metadata={CONSTANTS.EPOCH_NUM: epoch}, sync=False)

            eval_metrics = evaluate(flags, model, val_loader, loss_fn, score_fn, device, epoch)
            #eval_metrics["train_loss"] = sum(cumulative_loss) / len(cumulative_loss)
            eval_metrics["train_loss"] = tt[0] / len(cumulative_loss)            

            mllog_event(key=CONSTANTS.EVAL_ACCURACY,
                        value=eval_metrics["mean_dice"],
                        metadata={CONSTANTS.EPOCH_NUM: epoch},
                        sync=False)
            mllog_end(key=CONSTANTS.EVAL_STOP, metadata={CONSTANTS.EPOCH_NUM: epoch}, sync=False)

            for callback in callbacks:
                callback.on_epoch_end(epoch=epoch, metrics=eval_metrics, model=model, optimizer=optimizer)
            model.train()
            if eval_metrics["mean_dice"] >= flags.quality_threshold:
                is_successful = True
            elif eval_metrics["mean_dice"] < 1e-6:
                print("MODEL DIVERGED. ABORTING.")
                diverged = True

        mllog_end(key=CONSTANTS.BLOCK_STOP, sync=False,
                  metadata={CONSTANTS.FIRST_EPOCH_NUM: epoch, CONSTANTS.EPOCH_COUNT: 1})

        if is_successful or diverged:
            break

    mllog_end(key=CONSTANTS.RUN_STOP, sync=True,
              metadata={CONSTANTS.STATUS: CONSTANTS.SUCCESS if is_successful else CONSTANTS.ABORTED})
    for callback in callbacks:
        callback.on_fit_end()