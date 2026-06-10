"""Interactive "ask the globe" — natural-language Q&A over live clusters.

Pipeline (see ask.answer.answer_question):
  normalize → cache lookup → [miss] retrieve clusters → LLM synth → cache
with a degraded (templated, no-LLM) path that the endpoint falls back to
whenever the interactive LLM budget is spent or the model is slow/unavailable,
so /ask never 5xx's and never overruns the free tier.
"""
