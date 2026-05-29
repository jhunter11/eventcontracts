"""HuggingFace model support for the strategy pipeline.

Two distinct integration shapes, because HuggingFace covers two very different
model contracts:

1. **Text/transformer models** (``HfTextClassifier``). These consume tokenized
   text (``input_ids`` / ``attention_mask``), not the ``[N, n_features]``
   float tensor the Rust ``OnnxScorer`` serves. So they run in Python as
   *external-signal producers*: text → class probability →
   ``ExternalSignalEvent``, exactly the contract the tennis scorer uses to feed
   the Rust ``ExternalEdgeStrategy`` (see ``score_texts_to_signals``). This is
   how an earnings-guidance / headline-sentiment NLP model plugs into the
   existing strategy pipeline.

2. **Pre-exported single-tensor ONNX** hosted on the HuggingFace Hub
   (``resolve_hf_onnx``). A model whose ONNX graph already matches the
   single-float-tensor contract can be downloaded and served through the same
   generic ``predict_onnx`` / Rust ``OnnxScorer`` path as a local export.

``transformers``, ``torch``, ``optimum``, and ``huggingface_hub`` are optional
and imported lazily. Install them with ``requirements-hf.txt``; the base
framework never imports them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

import numpy as np

from eventcontracts.models.onnx_export import (
    METADATA_PREFIX,
    ModelFamily,
    ModelTask,
    embed_metadata,
)


@dataclass
class HfTextClassifier:
    """A loaded HuggingFace sequence-classification model + tokenizer.

    Use it offline/in-research to turn text into class probabilities. The
    positive-class probability becomes the fair value an external-edge strategy
    compares against the market quote.
    """

    model: Any
    tokenizer: Any
    positive_label_index: int = 1
    max_length: int = 256

    @classmethod
    def load(
        cls,
        model_id: str,
        *,
        revision: str | None = None,
        positive_label_index: int = 1,
        max_length: int = 256,
    ) -> HfTextClassifier:
        transformers = _import("transformers", "transformers")
        _import("torch", "torch")  # transformers needs a backend; fail early with a clear message.
        tokenizer = transformers.AutoTokenizer.from_pretrained(model_id, revision=revision)
        model = transformers.AutoModelForSequenceClassification.from_pretrained(model_id, revision=revision)
        model.eval()
        return cls(
            model=model,
            tokenizer=tokenizer,
            positive_label_index=positive_label_index,
            max_length=max_length,
        )

    def predict_proba(self, texts: Sequence[str]) -> np.ndarray:
        """Return class probabilities of shape ``[len(texts), n_classes]``."""

        torch = _import("torch", "torch")
        if not texts:
            raise ValueError("texts must be non-empty")
        encoded = self.tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        )
        with torch.no_grad():
            logits = self.model(**encoded).logits
            probabilities = torch.softmax(logits, dim=-1)
        return np.asarray(probabilities.detach().cpu().numpy(), dtype=np.float64)

    def positive_probability(self, texts: Sequence[str]) -> np.ndarray:
        """Positive-class probability, one value per input text."""

        proba = self.predict_proba(texts)
        if proba.ndim != 2 or proba.shape[1] <= self.positive_label_index:
            raise ValueError(f"unexpected probability shape {proba.shape}")
        return proba[:, self.positive_label_index]


def score_texts_to_signals(
    classifier: HfTextClassifier,
    items: Sequence[Mapping[str, Any]],
    *,
    source: str,
    schema_version: str = "hf-text-prediction-v1",
    text_field: str = "text",
    market_field: str = "market_id",
    probability_field: str = "probability",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Map ``{market_id, text}`` rows to ``ExternalSignalEvent``-shaped dicts.

    Mirrors the tennis scorer's JSONL output so an NLP signal feeds the same
    external-edge strategy contract the Rust runner already consumes. Each item
    must carry ``market_field`` and ``text_field``.
    """

    texts = [str(item[text_field]) for item in items]
    probabilities = classifier.positive_probability(texts)
    received_at = (now or datetime.now(UTC)).isoformat()
    events: list[dict[str, Any]] = []
    for item, probability in zip(items, probabilities, strict=True):
        market_id = str(item[market_field])
        payload = {market_field: market_id, probability_field: float(probability)}
        for key, value in item.items():
            if key not in (text_field,):
                payload.setdefault(key, value)
        events.append(
            {
                "event_kind": "external",
                "source": source,
                "schema_version": schema_version,
                "received_at": received_at,
                "payload": payload,
            }
        )
    return events


def export_hf_text_classifier_onnx(
    model_id: str,
    out_dir: str | Path,
    *,
    feature_schema_id: str,
    feature_schema_version: str,
    opset: int = 14,
    revision: str | None = None,
) -> Path:
    """Export a HF sequence-classification model to ONNX via ``optimum``.

    Returns the path to ``model.onnx``. The graph takes tokenized inputs
    (``input_ids`` / ``attention_mask``) and emits ``logits`` — a multi-input
    contract, NOT the single float-tensor contract of the tabular path. We
    still stamp the ``eventcontracts.*`` metadata (task + a note that this is a
    tokenized text model) so downstream tooling can tell the two apart.
    """

    optimum_main = _import("optimum.exporters.onnx", "optimum[exporters]").main_export
    onnx = _import("onnx", "onnx")
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    optimum_main(
        model_name_or_path=model_id,
        output=str(target_dir),
        task="text-classification",
        opset=opset,
        revision=revision,
    )
    model_path = target_dir / "model.onnx"
    if not model_path.exists():
        raise RuntimeError(f"optimum did not produce {model_path}")
    onnx_model = onnx.load(str(model_path))
    embed_metadata(
        onnx_model,
        {
            f"{METADATA_PREFIX}feature_schema_id": feature_schema_id,
            f"{METADATA_PREFIX}feature_schema_version": feature_schema_version,
            f"{METADATA_PREFIX}model_family": ModelFamily.HUGGINGFACE.value,
            f"{METADATA_PREFIX}task": ModelTask.BINARY_CLASSIFICATION.value,
            f"{METADATA_PREFIX}input_kind": "tokenized_text",
            f"{METADATA_PREFIX}input_names_json": json.dumps(["input_ids", "attention_mask"]),
            f"{METADATA_PREFIX}output_name": "logits",
            f"{METADATA_PREFIX}model_id": model_id,
        },
    )
    onnx.save_model(onnx_model, str(model_path))
    return model_path


def resolve_hf_onnx(
    repo_or_path: str | Path,
    *,
    filename: str = "model.onnx",
    revision: str | None = None,
) -> Path:
    """Resolve an ONNX file from a local path or the HuggingFace Hub.

    For a model whose ONNX graph already matches the single-float-tensor
    contract, this returns a local path the generic ``predict_onnx`` and Rust
    ``OnnxScorer`` can load directly. Downloads via ``huggingface_hub`` when the
    argument is a repo id rather than an existing local file.
    """

    candidate = Path(repo_or_path)
    if candidate.is_file():
        return candidate
    if candidate.is_dir():
        local = candidate / filename
        if local.is_file():
            return local
        raise FileNotFoundError(f"{filename} not found under {candidate}")
    hub = _import("huggingface_hub", "huggingface_hub")
    downloaded = hub.hf_hub_download(repo_id=str(repo_or_path), filename=filename, revision=revision)
    return Path(downloaded)


def _import(module_name: str, package_label: str) -> Any:
    try:
        return import_module(module_name)
    except ImportError as exc:  # pragma: no cover - exercised only without HF extras
        raise RuntimeError(
            f"{package_label} is required for HuggingFace model support; "
            "install the optional extras in requirements-hf.txt."
        ) from exc
