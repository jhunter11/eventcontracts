"""HuggingFace adapter tests.

These run without the optional HF extras installed: they assert the module
imports cleanly (no heavy deps at import time) and that the lazy import guards
fire with a clear, actionable message. The local-path branch of
``resolve_hf_onnx`` needs no HF deps at all.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from eventcontracts.models import hf

_HAS_TRANSFORMERS = importlib.util.find_spec("transformers") is not None
_HAS_HUB = importlib.util.find_spec("huggingface_hub") is not None


def test_hf_module_imports_without_heavy_deps() -> None:
    # Importing the module must not pull in transformers/torch/optimum.
    assert hasattr(hf, "HfTextClassifier")
    assert hasattr(hf, "export_hf_text_classifier_onnx")
    assert hasattr(hf, "resolve_hf_onnx")


def test_resolve_hf_onnx_returns_local_file(tmp_path: Path) -> None:
    model = tmp_path / "model.onnx"
    model.write_bytes(b"not-a-real-onnx-but-a-real-file")
    assert hf.resolve_hf_onnx(model) == model


def test_resolve_hf_onnx_finds_file_in_directory(tmp_path: Path) -> None:
    (tmp_path / "model.onnx").write_bytes(b"x")
    assert hf.resolve_hf_onnx(tmp_path) == tmp_path / "model.onnx"


def test_resolve_hf_onnx_missing_file_in_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        hf.resolve_hf_onnx(tmp_path)


@pytest.mark.skipif(_HAS_TRANSFORMERS, reason="transformers installed; guard not exercised")
def test_text_classifier_load_guards_missing_transformers() -> None:
    with pytest.raises(RuntimeError, match="requirements-hf.txt"):
        hf.HfTextClassifier.load("distilbert-base-uncased-finetuned-sst-2-english")


@pytest.mark.skipif(_HAS_TRANSFORMERS, reason="optimum/transformers installed; guard not exercised")
def test_export_text_classifier_guards_missing_optimum(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="requirements-hf.txt"):
        hf.export_hf_text_classifier_onnx(
            "distilbert-base-uncased",
            tmp_path,
            feature_schema_id="x",
            feature_schema_version="1",
        )


@pytest.mark.skipif(_HAS_HUB, reason="huggingface_hub installed; guard not exercised")
def test_resolve_hf_onnx_repo_guards_missing_hub() -> None:
    with pytest.raises(RuntimeError, match="requirements-hf.txt"):
        hf.resolve_hf_onnx("some-org/some-onnx-model")
