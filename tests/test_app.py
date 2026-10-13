import copy
import pytest
from security_lab.app import Chatbot, ProviderError, Settings, input_allowed, output_allowed
from security_lab.prompts import FAKE_SECRET, INPUT_QUESTIONS, OUTPUT_QUESTIONS

def choice(label="safe"):
    return {"classification": {"type": "choice", "choice": label, "confidence": 1,
            "probabilities": {k: float(k == label) for k in INPUT_QUESTIONS["classification"]["criteria"]}}}

class Stub:
    def __init__(self, label="safe", risk=0, fail=None):
        self.label, self.risk, self.fail = label, risk, fail
        self.seen = []
    def decide(self, state, questions):
        self.seen.append(("guard", state))
        if self.fail == "guard":
            raise ProviderError("simulated outage")
        if questions is INPUT_QUESTIONS:
            return choice(self.label), {}
        return {k: {"type": "noul", "noul": self.risk} for k in OUTPUT_QUESTIONS}, {}
    def chat(self, prompt):
        self.seen.append(("llm", prompt))
        if self.fail == "llm":
            raise ProviderError("simulated outage")
        return "Demo milk costs 6.50 SAR", {}

def test_baseline_never_calls_jev():
    provider = Stub(fail="guard")
    r = Chatbot(Settings(), provider).run("milk", "baseline")
    assert not r.error and [v[0] for v in provider.seen] == ["llm"]

@pytest.mark.parametrize("label", ["prompt_injection", "jailbreak", "suspicious_request"])
def test_input_block_prevents_llm(label):
    provider = Stub(label)
    r = Chatbot(Settings(), provider).run("bad", "input_output")
    assert r.response == "blocked" and r.blocked_stage == "input"
    assert len(provider.seen) == 1

def test_secret_only_reaches_output_guard():
    provider = Stub()
    r = Chatbot(Settings(), provider).run("milk", "input_output")
    assert r.response != "blocked"
    assert FAKE_SECRET not in str(provider.seen[0])
    assert provider.seen[2][1]["trusted_reference"]["protected_secret"] == FAKE_SECRET

def test_output_suppressed():
    r = Chatbot(Settings(), Stub(risk=0.9)).run("milk", "input_output")
    assert r.response == "blocked" and r.blocked_stage == "output" and r.candidate

@pytest.mark.parametrize("failure", ["llm", "guard"])
def test_failure_distinguished(failure):
    r = Chatbot(Settings(), Stub(fail=failure)).run("milk", "input")
    assert r.response == "blocked" and r.error and r.blocked_stage.endswith("_error")

@pytest.mark.parametrize("value", [float("nan"), -1, 2, "0.8", True])
def test_invalid_probability(value):
    a = choice()
    a["classification"]["probabilities"]["safe"] = value
    with pytest.raises(ProviderError):
        input_allowed(a, Settings())

def test_missing_output_decision():
    with pytest.raises(ProviderError):
        output_allowed({}, Settings())

def test_low_confidence_safe_is_allowed():
    a = choice()
    a["classification"]["confidence"] = 0.0
    assert input_allowed(a, Settings())

@pytest.mark.parametrize("safe,others", [(.63, [.05, 0, .32]), (.51, [.20, .10, .19]), (.30, [.25, .25, .20])])
def test_highest_safe_probability_passes_without_cutoff(safe, others):
    a = choice()
    a["classification"]["confidence"] = .01
    a["classification"]["probabilities"] = dict(zip(
        ['safe', 'prompt_injection', 'jailbreak', 'suspicious_request'], [safe, *others]))
    assert input_allowed(a, Settings())

def test_highest_unsafe_probability_blocks():
    a = choice('suspicious_request')
    a['classification']['probabilities'] = dict(safe=.30, prompt_injection=.20, jailbreak=.19, suspicious_request=.31)
    assert not input_allowed(a, Settings())

def test_input_tie_blocks():
    a = choice()
    a['classification']['probabilities'] = dict(safe=.5, prompt_injection=.5, jailbreak=0, suspicious_request=0)
    assert not input_allowed(a, Settings())

@pytest.mark.parametrize('probabilities,allowed', [
    (dict(safe=0, prompt_injection=.49, jailbreak=.01, suspicious_request=.50), False),
    (dict(safe=.51, prompt_injection=.49, jailbreak=0, suspicious_request=0), True),
])
def test_distribution_overrides_inconsistent_choice_label(probabilities, allowed):
    a = choice('prompt_injection')
    a['classification']['probabilities'] = probabilities
    assert input_allowed(a, Settings()) is allowed

@pytest.mark.parametrize('risk,allowed', [(.35, True), (.49, True), (.5, False), (.51, False), (1, False)])
def test_output_binary_highest_probability(risk, allowed):
    answers = {k: {'type': 'noul', 'noul': risk} for k in OUTPUT_QUESTIONS}
    assert output_allowed(answers, Settings()) is allowed

def test_canary_is_generated_without_a_committed_value():
    assert isinstance(FAKE_SECRET, str) and len(FAKE_SECRET) >= 24
