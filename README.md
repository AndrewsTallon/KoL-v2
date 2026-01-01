# KoL-v2

## DALI AI operator

This repository includes a small AI operator for the DALI lamp controller.

### Setup

1. Create a virtual environment (optional) and install dependencies:
   ```bash
   pip install -r requirements.txt
   # or minimal deps:
   pip install openai hid
   ```
2. Set your OpenAI API key (optional — falls back to a rules-based parser):
   ```bash
   export OPENAI_API_KEY=sk-...
   # Optional custom model:
   export OPENAI_MODEL=gpt-4o-mini
   ```

### Run

```bash
python -m dalicontrol.main              # interactive loop
python -m dalicontrol.main --dry-run    # log actions without touching hardware
```

Type natural language commands such as:
- `set to 30% and warm`
- `make it cool and max brightness`
- `turn off`
- `turn on`

Logs show the parsed actions, executed actions, and resulting lamp state. The last state
is persisted to `dalicontrol/state.json` so “turn on” restores the prior setting.
