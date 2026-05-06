#!/usr/bin/env python3
"""验证 SiliconFlow rerank API 的 relevance_score 是否不受 batch 组成影响。"""

import os
import sys
from pathlib import Path

import requests

API_KEY = os.getenv("SILICONFLOW_API_KEY", "").strip()
MODEL = os.getenv("RERANK_MODEL", "Qwen/Qwen3-Reranker-8B").strip()
if "/" not in MODEL:
    MODEL = f"Qwen/{MODEL}"
BASE_URL = "https://api.siliconflow.cn/v1"
ENDPOINT = f"{BASE_URL}/rerank"

QUERY = "multimodal information extraction from documents"

# 一些虚构文档，故意设计成相关性梯度明显
DOCS = {
    "A": "Title: Unified Multimodal Information Extraction\nAbstract: We propose a unified framework for extracting structured information from documents containing text, tables, and images.",
    "B": "Title: A Survey on Document Understanding\nAbstract: This paper surveys recent advances in document understanding, including layout analysis, OCR, and information extraction.",
    "C": "Title: Deep Learning for Image Classification\nAbstract: We present a convolutional neural network architecture for large-scale image classification on ImageNet.",
    "D": "Title: Reinforcement Learning for Robotics\nAbstract: We apply deep reinforcement learning to teach robots manipulation tasks in simulation and transfer to real world.",
    "E": "Title: Neural Machine Translation with Attention\nAbstract: We introduce an attention mechanism for neural machine translation that aligns source and target sentences.",
    "F": "Title: Table Structure Recognition Using Graph Neural Networks\nAbstract: We model table structure as a graph and use GNNs to recognize row/column relationships in document images.",
    "G": "Title: Weather Forecasting with Transformers\nAbstract: We apply transformer models to weather prediction using historical meteorological data.",
    "H": "Title: Cross-modal Retrieval for Document Information Extraction\nAbstract: We propose a cross-modal retrieval method that jointly uses text and visual features for document IE.",
}


def call_rerank(query: str, documents: list[str]) -> list[dict]:
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "query": query,
        "documents": documents,
    }
    resp = requests.post(ENDPOINT, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data.get("output"), dict):
        results = data.get("output", {}).get("results", [])
    else:
        results = data.get("results", [])
    return [item for item in results if isinstance(item, dict)]


def extract_scores(results: list[dict], doc_list: list[str]) -> dict[str, float]:
    scores = {}
    for item in results:
        idx = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        if idx is not None and score is not None and 0 <= idx < len(doc_list):
            scores[doc_list[idx]] = float(score)
    return scores


def main() -> None:
    if not API_KEY:
        print("ERROR: SILICONFLOW_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    print(f"Model: {MODEL}")
    print(f"Query: {QUERY}")
    print(f"Documents: {len(DOCS)} total")
    print()

    doc_keys = list(DOCS.keys())
    doc_texts = [DOCS[k] for k in doc_keys]

    # Test 1: 全部 8 篇一起
    print("=" * 60)
    print("Test 1: All 8 docs in one batch")
    print("=" * 60)
    results_all = call_rerank(QUERY, doc_texts)
    scores_all = extract_scores(results_all, doc_texts)
    for key in doc_keys:
        print(f"  doc {key}: {scores_all.get(DOCS[key], 'N/A'):.6f}")
    print()

    # Test 2: 每篇单独发
    print("=" * 60)
    print("Test 2: Each doc alone")
    print("=" * 60)
    scores_alone: dict[str, float] = {}
    for key in doc_keys:
        results = call_rerank(QUERY, [DOCS[key]])
        s = extract_scores(results, [DOCS[key]])
        score = s.get(DOCS[key], -1.0)
        scores_alone[key] = score
        print(f"  doc {key}: {score:.6f}")
    print()

    # Test 3: A 分别在不同 batch 里
    # batch1: A + C + D + G (不相关文档包围)
    # batch2: A + B + F + H (相关文档包围)
    # batch3: A 单独
    print("=" * 60)
    print("Test 3: Doc A in different batch contexts")
    print("=" * 60)

    batch1_keys = ["A", "C", "D", "G"]
    batch1_texts = [DOCS[k] for k in batch1_keys]
    results_b1 = call_rerank(QUERY, batch1_texts)
    s_b1 = extract_scores(results_b1, batch1_texts)
    score_a_in_b1 = s_b1.get(DOCS["A"], -1.0)
    print(f"  A in batch [A,C,D,G]: {score_a_in_b1:.6f}")

    batch2_keys = ["A", "B", "F", "H"]
    batch2_texts = [DOCS[k] for k in batch2_keys]
    results_b2 = call_rerank(QUERY, batch2_texts)
    s_b2 = extract_scores(results_b2, batch2_texts)
    score_a_in_b2 = s_b2.get(DOCS["A"], -1.0)
    print(f"  A in batch [A,B,F,H]: {score_a_in_b2:.6f}")

    score_a_alone = scores_alone["A"]
    print(f"  A alone:              {score_a_alone:.6f}")
    print()

    # 汇总对比
    print("=" * 60)
    print("Summary: Doc A score across contexts")
    print("=" * 60)
    contexts = {
        "all 8 docs": scores_all.get(DOCS["A"], -1.0),
        "with irrelevant [C,D,G]": score_a_in_b1,
        "with relevant [B,F,H]": score_a_in_b2,
        "alone": score_a_alone,
    }
    for label, score in contexts.items():
        print(f"  {label:30s} -> {score:.6f}")

    scores_list = list(contexts.values())
    spread = max(scores_list) - min(scores_list)
    print(f"\n  max - min spread: {spread:.6f}")
    if spread < 0.01:
        print("  CONCLUSION: relevance_score appears batch-independent (spread < 0.01)")
    elif spread < 0.05:
        print("  CONCLUSION: relevance_score has small batch dependence (spread < 0.05)")
    else:
        print("  CONCLUSION: relevance_score IS affected by batch composition (spread >= 0.05)")


if __name__ == "__main__":
    main()
