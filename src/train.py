# -*- coding: utf-8 -*-
"""Trainer for models.

- Author: Curt-Park
- Email: jwpark@jmarple.ai
"""

import os
from typing import Any, Dict

from progressbar import progressbar
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader as DataLoader
import wandb

from src.lr_schedulers import WarmupCosineLR
import src.utils as utils


class Trainer:
    """Trainer for models."""

    def __init__(
        self,
        model: nn.Module,
        trainloader: DataLoader,
        testloader: DataLoader,
        config: Dict[str, Any],
        dir_prefix: str,
        model_dir: str,
        device: torch.device,
        wandb_log: bool,
        wandb_init_params: Dict[str, Any],
    ) -> None:
        """Initialize."""
        self.model = model
        self.trainloader = trainloader
        self.testloader = testloader
        self.config = config
        self.wandb_log = wandb_log
        self.device = device
        self.init_params_path = ""
        self.reset(dir_prefix, model_dir)

        # create logger
        if wandb_log:
            wandb.init(**wandb_init_params)

    def reset(self, dir_prefix: str, model_dir: str) -> None:
        """Reset the configurations."""
        self.dir_prefix = dir_prefix
        self.model_dir = model_dir
        self.start_epoch = 0
        self.best_acc = 0.0

        # define criterion and optimizer
        self.criterion = nn.CrossEntropyLoss()
        self.optimizer = optim.SGD(
            self.model.parameters(),
            lr=self.config["LR"],
            momentum=self.config["MOMENTUM"],
            weight_decay=self.config["WEIGHT_DECAY"],
        )

        # learning rate scheduler
        self.lr_scheduler = WarmupCosineLR(
            self.config["WARMUP_EPOCHS"],
            self.config["EPOCHS"],
            self.config["START_LR"],
            self.config["LR"],
        )

        # initialize training
        if self.init_params_path:
            self.load_init_params(self.init_params_path)

        # best model path
        self.best_model_path = os.path.join(dir_prefix, model_dir)
        if not os.path.exists(self.best_model_path):
            os.mkdir(self.best_model_path)

    def run(self, log_data: Dict[str, float]) -> None:
        """Train the model."""
        if not self.init_params_path and self.config["STORE_PARAM_BEFORE"] == 0:
            self.save_init_params()

        for epoch in range(self.start_epoch, self.config["EPOCHS"]):
            self.lr_scheduler(self.optimizer, epoch)
            lr = self.optimizer.param_groups[0]["lr"]
            print(f"[INFO] Epoch: [{epoch} | {self.config['EPOCHS']-1}] LR: {lr}")

            # train
            train_loss = self.train_one_epoch()

            # test
            test_acc = self.test_one_epoch()
            print(f"[INFO] Test Accuracy: {test_acc:.2f}%\n")

            # save model
            self.save_best_params(test_acc, epoch)

            # store initial weights
            if (
                not self.init_params_path
                and self.config["STORE_PARAM_BEFORE"] - 1 == epoch
            ):
                self.save_init_params()

            # logging
            if self.wandb_log:
                log_data.update({"acc": test_acc, "loss": train_loss, "lr": lr})
                utils.wlog_pruned_weight(self.model)
                wandb.log(log_data)

    def train_one_epoch(self) -> float:
        """Train one epoch."""
        losses = []
        self.model.train()
        for data in progressbar(self.trainloader):
            # get the inputs; data is a list of [inputs, labels]
            images, labels = data[0].to(self.device), data[1].to(self.device)

            # zero the parameter gradients
            self.optimizer.zero_grad()

            # forward + backward + optimize
            outputs = self.model(images)
            loss = self.criterion(outputs, labels)
            loss.backward()
            self.optimizer.step()
            losses.append(loss.item())
        avg_loss = sum(losses) / len(losses)
        return avg_loss

    def test_one_epoch(self) -> float:
        """Test one epoch."""
        correct = total = 0
        self.model.eval()
        for data in progressbar(self.testloader):
            images, labels = data[0].to(self.device), data[1].to(self.device)
            outputs = self.model(images)
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
        test_acc = 100.0 * correct / total
        return test_acc

    def save_best_params(self, test_acc: float, epoch: int) -> None:
        """Save the model if the accuracy is better."""
        is_best = test_acc > self.best_acc
        self.best_acc = max(test_acc, self.best_acc)
        if is_best:
            self.save_params(self.best_model_path, str(epoch), epoch, test_acc)

    def save_init_params(self) -> None:
        """Set initial weights."""
        filename = "init_weight"
        print("[INFO] Stored initial weights\n")

        self.save_params(
            self.dir_prefix, filename, self.config["STORE_PARAM_BEFORE"] - 1,
        )
        self.init_params_path = os.path.join(self.dir_prefix, f"{filename}.pth.tar")

    def save_params(
        self, model_path: str, filename: str, epoch: int, test_acc: float = 0.0
    ) -> None:
        """Save model."""
        utils.save_checkpoint(
            {
                "state_dict": self.model.state_dict(),
                "optimizer": self.optimizer.state_dict(),
                "epoch": epoch,
                "test_acc": test_acc,
            },
            path=model_path,
            filename=f"{filename}.pth.tar",
        )
        print(f"[INFO] Saved model in {model_path}/{filename}.pth.tar\n")

    def load_params(self, model_path: str) -> None:
        """Load prameters."""
        checkpt = torch.load(model_path)
        self.initialize_params(self.model, checkpt["state_dict"])
        self.initialize_params(self.optimizer, checkpt["optimizer"])
        self.start_epoch = checkpt["epoch"] + 1
        print(f"[INFO] Loaded parameters from {model_path}\n")

    def load_init_params(self, init_params_path: str) -> None:
        """Load initial prameters."""
        self.load_params(init_params_path)
        self.start_epoch = self.config["PRUNE_START_FROM"]

    def initialize_params(self, model: Any, state_dict: Dict[str, Any]) -> None:
        """Initialize weights."""
        model_dict = self.model.state_dict()
        # 1. filter out unnecessary keys
        pretrained_dict = {k: v for k, v in state_dict.items() if k in model_dict}
        # 2. overwrite entries in the existing state dict
        model_dict.update(pretrained_dict)
        # 3. load the new state dict
        self.model.load_state_dict(model_dict)
