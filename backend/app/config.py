
import os

FEEDS = {
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "NPR Politics": "https://feeds.npr.org/1014/rss.xml",
}

# Initial heuristic weights, not a trained model.
KEYWORDS = {
    "election": 10,
    "president": 8,
    "congress": 10,
    "senate": 10,
    "parliament": 8,
    "government": 5,
    "nato": 10,
    "russia": 6,
    "ukraine": 6,
    "china": 6,
    "sanctions": 8,
    "foreign policy": 10,
    "supreme court": 10,
    "executive order": 10,
    "military": 5,
    "diplomacy": 8,
    "legislation": 8,
    "immigration": 5,
    "trade deal": 8,
    "geopolitics": 10,
}

OLLAMA_BASE_URL = os.getenv(
    "OLLAMA_BASE_URL",
    "http://localhost:11434",
)

OLLAMA_MODEL = os.getenv(
    "OLLAMA_MODEL",
    "qwen2.5:3b",
)
