import argparse
import glob
import os
import json
import time
import logging
import random
import re
from itertools import chain
from string import punctuation

import nltk

nltk.download("punkt")
from nltk.tokenize import sent_tokenize

import pandas as pd
import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader
import pytorch_lightning as pl


from transformers import AdamW, T5ForConditionalGeneration, T5Tokenizer, get_linear_schedule_with_warmup

from transformers import AutoTokenizer, AutoModelForSequenceClassification
from bleurt.score import BleurtScorer
from numpy import argmax
from math import exp

logging.basicConfig(level=logging.ERROR)
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"  # FATAL
os.environ["TOKENIZERS_PARALLELISM"] = "true"
logging.getLogger("tensorflow").setLevel(logging.FATAL)

bleurt_scorer = BleurtScorer("/home/animesh/MIforSE/bleurt-score/bleurt/bleurt-base-128/")
mi_tokenizer = AutoTokenizer.from_pretrained("ynie/roberta-large-snli_mnli_fever_anli_R1_R2_R3-nli")
mi_model = AutoModelForSequenceClassification.from_pretrained("ynie/roberta-large-snli_mnli_fever_anli_R1_R2_R3-nli")  # predicts E, N, C


def get_mi_score(s1, s2):  # returns average of s1 and s2
    tokenized_input_seq_pair = mi_tokenizer.encode_plus(s1, s2, max_length=256, return_token_type_ids=True, truncation=True)
    input_ids = torch.Tensor(tokenized_input_seq_pair["input_ids"]).long().unsqueeze(0)
    token_type_ids = torch.Tensor(tokenized_input_seq_pair["token_type_ids"]).long().unsqueeze(0)
    attention_mask = torch.Tensor(tokenized_input_seq_pair["attention_mask"]).long().unsqueeze(0)
    outputs = mi_model(
        input_ids,
        attention_mask=attention_mask,
        token_type_ids=token_type_ids,
        labels=None,
    )
    predicted_probability_12 = torch.softmax(outputs[0], dim=1)[0].tolist()  # batch_size only one

    tokenized_input_seq_pair = mi_tokenizer.encode_plus(s2, s1, max_length=256, return_token_type_ids=True, truncation=True)
    input_ids = torch.Tensor(tokenized_input_seq_pair["input_ids"]).long().unsqueeze(0)
    token_type_ids = torch.Tensor(tokenized_input_seq_pair["token_type_ids"]).long().unsqueeze(0)
    attention_mask = torch.Tensor(tokenized_input_seq_pair["attention_mask"]).long().unsqueeze(0)
    outputs = mi_model(
        input_ids,
        attention_mask=attention_mask,
        token_type_ids=token_type_ids,
        labels=None,
    )
    predicted_probability_21 = torch.softmax(outputs[0], dim=1)[0].tolist()  # batch_size only one

    return int(argmax(predicted_probability_12) == 0 and argmax(predicted_probability_21) == 0)


def get_bleurt(s1, s2):
    return (bleurt_scorer.score([s1], [s2])[0] + bleurt_scorer.score([s1], [s2])[0]) / 2


def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


set_seed(42)


class T5FineTuner(pl.LightningModule):
    def __init__(self, hparams):
        super(T5FineTuner, self).__init__()
        self.hparams = hparams

        self.model = T5ForConditionalGeneration.from_pretrained(hparams.model_name_or_path)
        self.tokenizer = T5Tokenizer.from_pretrained(hparams.tokenizer_name_or_path)

    def is_logger(self):
        return self.trainer.global_rank <= 0

    def forward(self, input_ids, attention_mask=None, decoder_input_ids=None, decoder_attention_mask=None, labels=None):
        return self.model(
            input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            labels=labels,
        )

    def _step(self, batch):
        labels = batch["target_ids"].detach().clone()
        labels[labels[:, :] == self.tokenizer.pad_token_id] = -100

        outputs = self(input_ids=batch["source_ids"], attention_mask=batch["source_mask"], labels=labels, decoder_attention_mask=batch["target_mask"])
        loss = outputs[0]

        batch_size = list(batch["source_ids"].size())[0]
        dollars = 0.0
        for i in range(batch_size):
            s1 = "".join(self.tokenizer.convert_ids_to_tokens(batch["source_ids"][i])).replace("<pad>", "").replace("<s>", "").replace("</s>", "").replace("paraphrase:", "").replace(u"\u2581", " ").strip()
            s2 = "".join(self.tokenizer.convert_ids_to_tokens(batch["target_ids"][i])).replace("<pad>", "").replace("<s>", "").replace("</s>", "").replace(u"\u2581", " ").strip()
            dollars += get_mi_score(s1, s2) / ((1 + exp(5 * get_bleurt(s1, s2))) ** 2)
        loss = torch.div(loss, dollars) if dollars >= 1 else torch.div(loss, 1.0)

        return loss

    def training_step(self, batch, batch_idx):
        loss = self._step(batch)

        tensorboard_logs = {"train_loss": loss}
        return {"loss": loss, "log": tensorboard_logs}

    def training_epoch_end(self, outputs):
        avg_train_loss = torch.stack([x["loss"] for x in outputs]).mean()
        tensorboard_logs = {"avg_train_loss": avg_train_loss}
        return {"avg_train_loss": avg_train_loss, "log": tensorboard_logs, "progress_bar": tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        loss = self._step(batch)
        return {"val_loss": loss}

    def validation_epoch_end(self, outputs):
        avg_loss = torch.stack([x["val_loss"] for x in outputs]).mean()
        tensorboard_logs = {"val_loss": avg_loss}
        return {"avg_val_loss": avg_loss, "log": tensorboard_logs, "progress_bar": tensorboard_logs}

    def configure_optimizers(self):
        "Prepare optimizer and schedule (linear warmup and decay)"

        model = self.model
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": self.hparams.weight_decay,
            },
            {
                "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=self.hparams.learning_rate, eps=self.hparams.adam_epsilon)
        self.opt = optimizer
        return [optimizer]

    def optimizer_step(self, epoch, batch_idx, optimizer, optimizer_idx, second_order_closure=None):
        if self.trainer.use_tpu:
            xm.optimizer_step(optimizer)
        else:
            optimizer.step()
        optimizer.zero_grad()
        self.lr_scheduler.step()

    def get_tqdm_dict(self):
        tqdm_dict = {"loss": "{:.3f}".format(self.trainer.avg_loss), "lr": self.lr_scheduler.get_last_lr()[-1]}

        return tqdm_dict

    def train_dataloader(self):
        train_dataset = get_dataset(tokenizer=self.tokenizer, type_path="train", args=self.hparams)
        dataloader = DataLoader(train_dataset, batch_size=self.hparams.train_batch_size, drop_last=True, shuffle=True, num_workers=4)
        t_total = (len(dataloader.dataset) // (self.hparams.train_batch_size * max(1, self.hparams.n_gpu))) // self.hparams.gradient_accumulation_steps * float(self.hparams.num_train_epochs)
        scheduler = get_linear_schedule_with_warmup(self.opt, num_warmup_steps=self.hparams.warmup_steps, num_training_steps=t_total)
        self.lr_scheduler = scheduler
        return dataloader

    def val_dataloader(self):
        val_dataset = get_dataset(tokenizer=self.tokenizer, type_path="val", args=self.hparams)
        return DataLoader(val_dataset, batch_size=self.hparams.eval_batch_size, num_workers=4)


logger = logging.getLogger(__name__)


class LoggingCallback(pl.Callback):
    def on_validation_end(self, trainer, pl_module):
        logger.info("***** Validation results *****")
        if pl_module.is_logger():
            metrics = trainer.callback_metrics
            # Log results
            for key in sorted(metrics):
                if key not in ["log", "progress_bar"]:
                    logger.info("{} = {}\n".format(key, str(metrics[key])))

    def on_test_end(self, trainer, pl_module):
        logger.info("***** Test results *****")

        if pl_module.is_logger():
            metrics = trainer.callback_metrics

            # Log and save results to file
            output_test_results_file = os.path.join(pl_module.hparams.output_dir, "test_results.txt")
            with open(output_test_results_file, "w") as writer:
                for key in sorted(metrics):
                    if key not in ["log", "progress_bar"]:
                        logger.info("{} = {}\n".format(key, str(metrics[key])))
                        writer.write("{} = {}\n".format(key, str(metrics[key])))


args_dict = dict(
    data_dir="paraphrase_data",  # path for data files
    output_dir="t5_paraphrase1",  # path to save the checkpoints
    model_name_or_path="t5_paraphrase1/model",
    tokenizer_name_or_path="t5-base",
    max_seq_length=256,
    learning_rate=3e-4,
    weight_decay=0.0,
    adam_epsilon=1e-8,
    warmup_steps=0,
    train_batch_size=20,
    eval_batch_size=20,
    num_train_epochs=4,
    gradient_accumulation_steps=2,
    n_gpu=1,
    early_stop_callback=False,
    fp_16=False,  # if you want to enable 16-bit training then install apex and set this to true
    opt_level="O1",  # you can find out more on optimisation levels here https://nvidia.github.io/apex/amp.html#opt-levels-and-properties
    max_grad_norm=1.0,  # if you enable 16-bit training then set this to a sensible value, 0.5 is a good default
    seed=42,
)

train_path = "paraphrase_data/train.tsv"
val_path = "paraphrase_data/val.tsv"

train = pd.read_csv(train_path, sep="\t")
print(train.head())

tokenizer = T5Tokenizer.from_pretrained("t5-base")


class ParaphraseDataset(Dataset):
    def __init__(self, tokenizer, data_dir, type_path, max_len=256):
        self.path = os.path.join(data_dir, type_path + ".tsv")

        self.source_column = "sentence1"
        self.target_column = "sentence2"
        self.data = pd.read_csv(self.path, sep="\t")

        self.max_len = max_len
        self.tokenizer = tokenizer
        self.inputs = []
        self.targets = []

        self._build()

    def __len__(self):
        return len(self.inputs)

    def __getitem__(self, index):
        source_ids = self.inputs[index]["input_ids"].squeeze()
        target_ids = self.targets[index]["input_ids"].squeeze()

        src_mask = self.inputs[index]["attention_mask"].squeeze()  # might need to squeeze
        target_mask = self.targets[index]["attention_mask"].squeeze()  # might need to squeeze

        return {"source_ids": source_ids, "source_mask": src_mask, "target_ids": target_ids, "target_mask": target_mask}

    def _build(self):
        for idx in range(len(self.data)):
            input_, target = self.data.loc[idx, self.source_column], self.data.loc[idx, self.target_column]

            input_ = "paraphrase: " + str(input_) + " </s>"
            target = str(target) + " </s>"

            # tokenize inputs
            tokenized_inputs = self.tokenizer.batch_encode_plus([input_], max_length=self.max_len, pad_to_max_length=True, return_tensors="pt")
            # tokenize targets
            tokenized_targets = self.tokenizer.batch_encode_plus([target], max_length=self.max_len, pad_to_max_length=True, return_tensors="pt")

            self.inputs.append(tokenized_inputs)
            self.targets.append(tokenized_targets)


if not os.path.exists("t5_paraphrase1"):
    os.makedirs("t5_paraphrase1")

args = argparse.Namespace(**args_dict)
print(args_dict)

checkpoint_callback = pl.callbacks.ModelCheckpoint(filepath=args.output_dir, prefix="checkpoint", monitor="val_loss", mode="min", save_top_k=5)

train_params = dict(
    accumulate_grad_batches=args.gradient_accumulation_steps,
    gpus=args.n_gpu,
    max_epochs=args.num_train_epochs,
    early_stop_callback=False,
    precision=16 if args.fp_16 else 32,
    amp_level=args.opt_level,
    gradient_clip_val=args.max_grad_norm,
    checkpoint_callback=checkpoint_callback,
    callbacks=[LoggingCallback()],
)


def get_dataset(tokenizer, type_path, args):
    return ParaphraseDataset(tokenizer=tokenizer, data_dir=args.data_dir, type_path=type_path, max_len=args.max_seq_length)


print("Initialize model")
model = T5FineTuner(args)

trainer = pl.Trainer(**train_params)

print(" Training model")
trainer.fit(model)

print("training finished")

print("Saving model")
model.model.save_pretrained("t5_paraphrase1")

print("Saved model")
