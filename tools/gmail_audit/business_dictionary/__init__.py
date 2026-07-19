"""Business Dictionary — structured business glossary extracted from Drive, emails, attachments.

Architecture: PostgreSQL for structured storage + Neo4j for graph relationships between terms.
No Chroma dependency — extraction uses LLM-as-judge on already-ingested documents.
"""
