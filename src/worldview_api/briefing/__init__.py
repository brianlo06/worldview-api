"""Top-stories briefing narration (POST /briefing).

Turns the top clusters into a short, natural, spoken-word script via the
existing OpenAI-compatible LLM client, with a cleaned-up no-LLM fallback so the
briefing always plays. See narrate.py.
"""
