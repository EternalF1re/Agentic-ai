# Agent Harness Demo

A local Python demo of an agent runtime with:

- Supervisor / Worker delegation
- short-term conversation state
- local long-term memory persistence
- checkpoint resume
- plan-act-replan control flow
- lifecycle hooks
- fail-closed human approval for high-risk tool calls

The demo uses an OpenAI-compatible local model endpoint. The default configuration expects Ollama at `http://localhost:11434/v1` and model `qwen2.5:7b`.

## Requirements

- Python 3.10+
- Ollama installed locally
- The default local model:

```bash
ollama pull qwen2.5:7b
ollama serve
```

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python workspace\agent_harness_demo.py
```

Type `exit` or `quit` to leave the interactive shell.

## Example Prompts

```text
你好，请问 1+1 等于几？另外告诉我什么是多智能体委派架构？
```

```text
请读取 workspace/task_details.txt，然后告诉我里面的核心结论。
```

```text
请先帮我查一下成都明天的天气预报，然后把天气结论写入到 workspace/weather_report.txt 文件里。
```

High-risk operations such as writing files are suspended for approval. When prompted, use:

```text
approve <approval_id>
reject <approval_id> <reason>
```

## Runtime Files

The script may create local runtime files under `workspace/`, such as:

- `ltm.json`
- `state_checkpoint.json`
- `approval_requests.json`
- `weather_report.txt`

These are intentionally ignored by Git.
