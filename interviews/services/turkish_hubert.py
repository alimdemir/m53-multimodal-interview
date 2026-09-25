"""Inference-only architecture for SeaBenSea's Turkish HuBERT checkpoint.

Checkpoint layout verified against its config and the publisher's HuBERT-SER
src/models.py. No downloaded Python code is executed. Single, unpadded utterance.
"""
import torch
from torch import nn
from transformers import HubertModel, HubertPreTrainedModel
from transformers.modeling_outputs import SequenceClassifierOutput


class ClassificationHead(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.out_proj = nn.Linear(config.hidden_size, config.num_labels)

    def forward(self, features):
        return self.out_proj(torch.tanh(self.dense(features)))


class TurkishHubertClassifier(HubertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        if getattr(config, "pooling_mode", None) != "mean":
            raise ValueError("Bu adaptör yalnız mean pooling checkpoint destekler.")
        self.hubert = HubertModel(config)
        self.classifier = ClassificationHead(config)
        self.post_init()

    def forward(self, input_values, attention_mask=None):
        hidden = self.hubert(input_values, attention_mask=attention_mask).last_hidden_state
        return SequenceClassifierOutput(logits=self.classifier(hidden.mean(dim=1)))
