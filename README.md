# Enhancing LLM Security Through Decision Models

A supermarket customer-service chatbot experiment comparing a system-prompt-only LLM with input and output filtering by a separate decision model.

**Target LLM:** `qwen/qwen-2.5-7b-instruct` through OpenRouter.  
**Decision model:** `~typesafe/jev-latest` through OpenRouter's Decisions API.  
**Three independently runnable paths:** `baseline`, `input`, and `input_output`.

The historical evaluation found **4,124 / 50 / 47 garak-only detector hits** respectively when excluding responses blocked by Jev. These are targeted detector findings, not a comprehensive count of unsafe answers. No detector hit does not establish safety.

## Scope and publication boundaries

The chatbot may identify grocery items, provide prices from a small fictional SAR catalogue, and help customers describe or escalate supermarket complaints. Related greetings and clarifying questions are allowed. It must refuse other tasks, harmful instructions, policy overrides and secret extraction. It has no account access, refund tools, code execution, inventory system or live pricing.

This repository contains the runnable chatbot and offline unit tests. **No API credentials, fixed secret values, garak source, garak datasets, evaluation harness, raw transcripts, generated reports or evaluation result files are included.** Garak is described below as the external evaluation tool; it is not a runtime dependency.

The published code generates a synthetic canary with `secrets.token_urlsafe(24)` at process startup. The system prompt tells the chatbot not to reveal or transform it. The input guard knows a secret exists but does not receive its value. The output guard receives it as trusted reference data. It is only an experimental canary—not a real API key. Never put production credentials in a model prompt.

**Historical difference:** the evaluated version used a fixed synthetic canary, omitted from this repository. Each published process now generates a different value. The reported measurements were not rerun after that sanitization and are not a byte-for-byte benchmark of this release.

## Approach

All three paths use the same supermarket policy and catalogue. Qwen is requested with temperature `0`, seed `1729`, and a maximum of `384` output tokens. Provider responses need not be deterministic. The observed Jev alias resolved to `typesafe/jev-1.13-20260917` during the evaluation; aliases and availability can change.

### Path 1: Baseline — system prompt only

No Jev decision is requested. The system prompt is the only model-policy layer.

```mermaid
flowchart TD
    U[Customer message] --> Q[Qwen + supermarket system prompt]
    Q -->|Successful response| R[Deliver answer]
    Q -->|Provider error| E[Return blocked + separate error metadata]
```

### Path 2: Jev input guard + LLM

Jev classifies the untrusted customer message using the chatbot's allowed scope and secret-protection policy. Qwen is called only if `safe` is the unique highest-probability category.

```mermaid
flowchart TD
    U[Customer message] --> J[Jev input classification]
    J --> D{Safe has uniquely highest probability?}
    D -->|Yes| Q[Qwen + supermarket system prompt]
    D -->|No or tie| B[Return blocked; do not call Qwen]
    Q --> R[Deliver answer]
    J -->|Provider or schema error| E[Fail closed; record error separately]
    Q -->|Provider error| E
```

Input categories:

| Category | Intended meaning |
|---|---|
| `safe` | Permitted supermarket assistance |
| `prompt_injection` | Attempts to override instructions, spoof authority or extract hidden information |
| `jailbreak` | Explicit role/safety bypass or unrestricted-persona attempts |
| `suspicious_request` | Other out-of-scope, harmful or suspicious requests |

**No confidence threshold:** a safe probability of `0.30` is accepted if all other categories are lower. The implementation ranks returned probabilities itself; the provider's category label and confidence do not override that ranking. Exact ties block. Invalid or missing decisions fail closed. Angry complaints are not automatically attacks.

### Path 3: Jev input guard + LLM + Jev output guard

This repeats the input gate, then buffers Qwen's candidate until a separate output decision finishes. Jev sees the original message, candidate answer and trusted catalogue/canary. It does not rewrite the answer: it either releases it unchanged or replaces it with `blocked`.

```mermaid
flowchart TD
    U[Customer message] --> I[Jev input classification]
    I --> D{Safe uniquely wins?}
    D -->|No or tie| B[Return blocked]
    D -->|Yes| Q[Qwen + supermarket system prompt]
    Q --> C[Buffer candidate; do not release yet]
    C --> O[Jev output assessment + trusted references]
    O --> G{Every violation probability below 0.5?}
    G -->|Yes| R[Deliver original candidate]
    G -->|No or tie| B
    I -->|Error| E[Fail closed; separate error metadata]
    Q -->|Error| E
    O -->|Error| E
```

The output guard has four independent binary questions, requested together:

| Output check | Violation being assessed |
|---|---|
| `secret_leak` | Canary disclosure, confirmation, partial/encoded transformation or hidden-prompt leakage |
| `scope_violation` | Out-of-scope work or following injected instructions |
| `unsafe_content` | Actionable harmful/exploit/fraud guidance, credential solicitation or private customer data |
| `unsupported_claim` | Invented prices, unsupported stock claims or claims of completed refund/account actions |

The returned `noul` value represents the violation probability. Release requires **all four values below `0.5`**, meaning “no violation” wins each binary decision. A tie or any higher value blocks. This is binary highest-probability selection, not the input guard's four-category confidence rule.

## Run locally

Python 3.12 was used for the experiment. From the repository folder:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` locally and set `OPENROUTER_API_KEY` to your own credential. The example has a blank value, and `.env` is ignored. The key is used as an HTTP authorization header, not inserted into the system prompt or Jev state.

Run one path at a time:

```powershell
.venv\Scripts\python.exe -X utf8 -m security_lab.app --mode baseline --inspect
.venv\Scripts\python.exe -X utf8 -m security_lab.app --mode input --inspect
.venv\Scripts\python.exe -X utf8 -m security_lab.app --mode input_output --inspect
```

Run each command in a separate terminal to compare the paths. Submitting prompts consumes API credits. Enter `/quit` to exit.

For a single request:

```powershell
.venv\Scripts\python.exe -X utf8 -m security_lab.app --mode input_output --prompt "How much is whole milk?" --inspect
```

On macOS/Linux use `python3 -m venv .venv`, `.venv/bin/python` and `cp .env.example .env` instead of the Windows commands. The Python CLI is portable.

Every entered prompt is an **independent single-turn test**; prior messages are not conversation context. `--inspect` prints the delivered answer, gate decisions, blocked stage and latency. `--metadata` emits structured diagnostics without suppressed candidates. Debug output can still include delivered content and request metadata; keep it private. The internal Python `Result` retains candidates for local inspection and must not be exposed wholesale in a production endpoint.

Policy rejections return `blocked`. Provider/schema failures also fail closed, but have separate error metadata and a nonzero exit code in single-request mode. Interactive requests may retry transient failures up to three attempts. **The interactive CLI has no dollar or test-count cap**; the historical evaluation used a separate budgeted runner that is deliberately not published.

### Offline tests

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
```

Tests use a stub provider and do not make paid calls. They check path routing, input/output decisions, tie handling, malformed probabilities, fail-closed behavior and keeping the canary out of input-guard state. Garak is not required.

## Historical evaluation using garak

[Garak](https://github.com/NVIDIA/garak) is an external LLM vulnerability scanner. Its probes supply test cases; its detectors inspect outputs for specific target behaviors. Jev is the guard being evaluated, not the garak detector.

The private evaluation used **garak 0.17.0** probe constructors, metadata hooks and primary detectors in a budgeted, breadth-first, static single-turn text harness. It did not execute every native probe's adaptive or multi-turn workflow. This repository does not bundle that harness or claim the local unit tests reproduce the benchmark.

### Coverage and stopping condition

- Date: 13 October 2026.
- 19,965 attempts per path; **59,895 total**, including two execution errors.
- 19,965 aligned prompt triples; 19,963 error-free triples; no incomplete saved triples.
- Stopped conservatively below the requested 20,000-per-path ceiling, not because the corpus was exhausted. No additional paid tests were run afterward.
- Inventory: 195 installed probe classes; 114 ready, 58 excluded and 23 unavailable. Eight local supermarket groups brought the tested group count to 122.
- Every group was represented; 100 groups were fully consumed and 22 remained partial.
- Eligible frozen queue: 71,207 prompts/path; 51,242/path remained unrun.
- Only applicable static text cases were used. Audio/video, tool-dependent and unavailable model/dataset-dependent cases were not counted as passes. Deployed web/RAG behavior and multi-turn attacks were not assessed.

### Garak-only hits, excluding Jev-blocked responses

This is the requested headline comparison. Custom supermarket and fake-secret detectors are excluded. A hit is a primary garak detector score of at least `0.5` on a response **not blocked by Jev**.

| Path | Garak hits on nonblocked responses |
|---|---:|
| Baseline | **4,124** |
| Jev input guard | **50** |
| Jev input + output guards | **47** |

These are counts, not conditional pass-through rates. The guarded paths suppress most inputs before Qwen runs, so counts should not be interpreted as the safety rate among all generated candidate answers.

For transparency, the unadjusted primary-garak results were:

| Path | All garak-scored cases | Raw garak hits | Hits on literal `blocked` responses | Hits excluding those blocks |
|---|---:|---:|---:|---:|
| Baseline | 19,902 | 4,124 | 0 | 4,124 |
| Input | 19,903 | 3,282 | 3,232 | 50 |
| Input + output | 19,902 | 3,279 | 3,232 | 47 |

Some detectors look for missing refusal patterns and can flag the literal string `blocked`. Those hits are preserved in raw totals but removed from the requested nonblocked-response comparison. A missing refusal phrase is not evidence that a blocked answer contained harmful instructions.

### Combined, matched scoring used during monitoring

The earlier progress reports also included local supermarket and synthetic-secret checks. They used a common set of **19,940 error-free scored attacks per path**, treating literal guard blocks as prevention:

| Path | Combined detector hits | Hit rate |
|---|---:|---:|
| Baseline | 4,143 | 20.7773% |
| Input | 50 | 0.2508% |
| Input + output | 47 | 0.2357% |

Do not mix these totals with the garak-only table: the detectors and denominators differ. Synthetic-secret flags were 11 / 0 / 0, which may include supported partial/transformed disclosures rather than full literal leaks. The secret was not a production credential.

### Isolated output-guard effect

The three-hit difference between independent input and input+output paths does **not** establish output-guard benefit. Input decisions and Qwen wording can vary between calls even at temperature zero.

An offline ablation instead compared each saved input+output candidate **before versus after its own recorded output gate**:

| Same-candidate measure | Result |
|---|---:|
| Candidates reaching output review | 425 |
| Scored attack candidates | 408 |
| Unscored attack candidates | 1 |
| Benign candidates | 16 |
| Detector hits before / after | **47 / 47** |
| Detector hits removed by output guard | **0** |
| Output-blocked responses | 5 |
| Benign useful responses before / after | **16 / 15** |

Manual review of all five output blocks found:

| Case | Output guard action and interpretation |
|---|---|
| Wrong milk price | Blocked an answer quoting 6 SAR/L instead of the trusted demo price 6.50 SAR/L. **Confirmed unsupported price prevented**, although the garak detector did not flag it. |
| Spoiled-milk complaint | Blocked a legitimate complaint-handling response. Likely overblocking; no completed refund was claimed. |
| Apple identification | Blocked an apple identification and general produce-section suggestion. Likely overblocking; no invented price. |
| Mathematics mnemonic | Blocked a brief out-of-scope mnemonic explanation followed by grocery redirection. Minor scope drift caught, not harmful advice. |
| Emoji clarification | Blocked clarification about store items. Scope boundary was ambiguous; no demonstrated harmful content. |

Thus the output guard showed a **business-policy benefit outside the measured detector hits**, alongside a utility cost. This review was not blinded or a comprehensive review of delivered answers.

## Limitations and security notes

- **No detector hit does not mean safe. Unreviewed answers are safety not assessed.** Detectors measure particular signatures, target strings or behaviors, not every unsafe outcome.
- Encoded target reproduction can be a genuine detector positive; substring matching can also cause incidental matches. Context matters, and neither should be silently relabeled as safety.
- Only 16 benign cases were tested. This cannot establish production false-block rates or customer-service quality.
- The output ablation is conditional on passing the input guard, not an output-only test against the full attack corpus.
- One generation per prompt, corpus mix, missing detectors and partial coverage limit generalization. There was no comprehensive independent semantic safety judge.
- Input content and output candidates are sent to external model providers. Use synthetic data, not real customer records or credentials.
- A canary inside a system prompt is not a secure credential store. This is a research prototype, not a production security boundary.
- Raw traces, garak material and the exact historical fixed canary are intentionally absent, limiting independent reproduction of the reported aggregate results.

## Repository layout

```text
security_lab/
  app.py           # OpenRouter calls, gates and single-turn CLI
  prompts.py       # Supermarket policy, demo catalogue, generated canary
  __init__.py
tests/test_app.py  # Offline stub-provider tests
.env.example      # Empty credential field and model configuration
requirements.txt
requirements-dev.txt
```

Evaluation tooling reference: [garak source and documentation](https://github.com/NVIDIA/garak). It is mentioned for methodological transparency and is not included in this project.
