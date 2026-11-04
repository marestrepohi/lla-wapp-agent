"""Extract, chunk, embed, and replace the public Qdrant RAG corpus."""

from __future__ import annotations

import argparse
import os
import sys

from lla_langchain_agent.qdrant_rag import DEFAULT_WEB_COLLECTION, QdrantWebCorpus
from lla_langchain_agent.embeddings import FastEmbedEmbeddings
from lla_langchain_agent.rag import load_web_pages, split_documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="+", help="Public HTTPS pages to index.")
    parser.add_argument(
        "--collection",
        default=os.getenv("QDRANT_WEB_COLLECTION", DEFAULT_WEB_COLLECTION),
    )
    args = parser.parse_args()

    qdrant_url = os.getenv("QDRANT_URL", "").strip()
    qdrant_api_key = os.getenv("QDRANT_API_KEY", "").strip()
    if not qdrant_url or not qdrant_api_key:
        parser.error("set QDRANT_URL and QDRANT_API_KEY in the environment")

    documents = load_web_pages(args.urls)
    chunks = split_documents(documents)
    embeddings = FastEmbedEmbeddings()
    corpus = QdrantWebCorpus(
        url=qdrant_url,
        api_key=qdrant_api_key,
        collection_name=args.collection,
        embedding=embeddings,
    )
    ids = corpus.replace(chunks, embeddings)
    print(f"Indexados {len(ids)} fragmentos en la colección {args.collection}.")
    for document in documents:
        print(f"Fuente: {document.metadata.get('source', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
