import os
import fire
import torch
from torch.nn.parallel import DistributedDataParallel as DDP
from typing import Literal, Union
from transformers import AutoModelForCausalLM
import numpy as np
from contextlib import nullcontext
from utils import (
    print_trainable_parameters,
    forward_batch
)
from data import get_dataloader
from models import (
    get_model_and_tokenizer,
    get_optimizer_for_model
)
from torch.utils.tensorboard import SummaryWriter
writer = SummaryWriter()


def init_distributed(rank: int):
    torch.distributed.init_process_group(
        backend='nccl',
        init_method='env://'
    )
    torch.cuda.set_device(rank)

    # return world size
    torch.distributed.get_world_size()

def train(
    model_name: str,
    datasets: Union[str, list[str]], # path to dataset(s) on huggingface. must have (prompt, chosen, rejected)
    num_epochs: int = 1,
    quantization: Literal["4bit", "8bit", None] = None,
    loss_fn: Literal["dpo", "sft"] = "dpo",
    batch_size: int = 16,
    accum_steps: int = 1,
    lr: float = 2.0e-5,
    num_workers: int = 4,
    save_dir: str = "checkpoint-final",
):

    # get LoRA model
    model, tokenizer = get_model_and_tokenizer(
        model_name=model_name,
        gradient_checkpointing=True,
        load_in_4bit=(quantization == "4bit"),
        load_in_8bit=(quantization == "8bit"),
        lora=True,
        lora_ckpt=None,
        device=None,
    )
        

    # get train dataloader
    datasets = [datasets] if isinstance(datasets, str) else datasets
    dataloader = get_dataloader(
        dataset_names=datasets,
        tokenizer=tokenizer,
        batch_size=batch_size,
        num_workers=num_workers,
        distributed=False,
    )

    # get optimizer
    optimizer = get_optimizer_for_model(
        model, model_name, max_lr=lr
    )

    # train -- uh oh what do????
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    # set to float if not on cuda
    if device == torch.device("cpu"):
        model = model.float()
    if quantization not in ["4bit", "8bit"]:
        # if using bitsandbytes the model will already be on the right device
        model.to(device)
    

    # train loop
    model.train()
    for epoch in range(num_epochs):
        print(f"=== Epoch {epoch} ===")
        for i, batch in enumerate(dataloader):
            loss, metrics = forward_batch(model, batch, device, loss_fn=loss_fn, train=True)
            for metric in metrics:
                # if it's a list take the mean otherwise just log as is
                if isinstance(metrics[metric], list):
                    writer.add_scalar(metric, np.mean(metrics[metric]), i)
                else:
                    writer.add_scalar(metric, metrics[metric], i)
            (loss / accum_steps).backward()

            if (i + 1) % accum_steps == 0:
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)

        # save model at the end
        model.save_pretrained(save_dir + f"_epoch_{epoch + 1}")

def train_ddp(
    model_name: str,
    datasets: Union[str, list[str]], # path to dataset(s) on huggingface. must have (prompt, chosen, rejected)
    num_epochs: int = 1,
    quantization: Literal["4bit", "8bit", None] = None,
    loss_fn: Literal["dpo", "sft"] = "dpo",
    batch_size: int = 16,
    accum_steps: int = 1,
    lr: float = 2.0e-5,
    num_workers: int = 4,
    save_dir: str = "checkpoint-final",
    rank: int = None,
):
    # initialize distributed
    if rank is None:
        rank = os.environ.get("RANK", None)
        if rank is None:
            raise ValueError("Couldn't get rank.")
    print(f"Hello from device {rank}!")
    world_size = init_distributed(rank)
    assert world_size > 1, "Must have more than one GPU to use DDP"
    assert accum_iters % world_size == 0, "Accumulation steps must be divisible by world size"
    accum_iters = accum_iters // world_size # we want total accumulation steps to be the same no matter hardware

    # get LoRA model
    model, tokenizer = get_model_and_tokenizer(
        model_name=model_name,
        gradient_checkpointing=True,
        load_in_4bit=(quantization == "4bit"),
        load_in_8bit=(quantization == "8bit"),
        lora=True,
        lora_ckpt=None,
        device=f"cuda:{rank}",
    )

    # get train dataloader
    datasets = [datasets] if isinstance(datasets, str) else datasets
    dataloader = get_dataloader(
        dataset_names=datasets,
        tokenizer=tokenizer,
        batch_size=batch_size,
        num_workers=num_workers,
        distributed=False,
    )

    # get optimizer
    optimizer = get_optimizer_for_model(
        model, model_name, max_lr=lr
    )

    # train -- uh oh what do????
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    # set to float if not on cuda
    if device == torch.device("cpu"):
        model = model.float()
    if quantization not in ["4bit", "8bit"]:
        # if using bitsandbytes the model will already be on the right device
        model.to(device)
    

    # train loop
    model.train()
    for epoch in range(num_epochs):
        dataloader.sampler.set_epoch(epoch)
        print(f"=== Epoch {epoch} ===")
        for i, batch in enumerate(dataloader):
            with model.no_sync() if (i + 1) % accum_steps != 0 else nullcontext():
                loss, metrics = forward_batch(model, batch, device, loss_fn=loss_fn, train=True)
                for metric in metrics:
                    # if it's a list take the mean otherwise just log as is
                    if isinstance(metrics[metric], list):
                        writer.add_scalar(metric, np.mean(metrics[metric]), i)
                    else:
                        writer.add_scalar(metric, metrics[metric], i)
                (loss / accum_steps).backward()

            if (i + 1) % accum_steps == 0:
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)

        # save model at the end, but only rank 0
        if torch.distributed.get_rank() == 0:
            model.save_pretrained(save_dir + f"_epoch_{epoch + 1}")

    
    
    
if __name__ == "__main__":
    fire.Fire()
