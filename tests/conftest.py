"""Load the plugin from its hyphenated directory, isolate HERMES_HOME, and provide a fake host."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "hermes_claude_agents"
DIRECT = "claude-subscription-directsdk-experimental"
sys.dont_write_bytecode = True


def _hermes_source() -> str:
    """Hermes checkout for the integration tests: HERMES_SOURCE, else the checkout of an installed Hermes.

    An editable install only maps Hermes' packages; root modules such as hermes_yaml need the
    checkout itself on sys.path.
    """
    if os.environ.get("HERMES_SOURCE"):
        return os.environ["HERMES_SOURCE"]
    try:
        import hermes_cli
    except ImportError:
        return ""
    root = Path(hermes_cli.__file__).resolve().parents[1]
    return str(root) if (root / "hermes_constants.py").exists() else ""


HERMES_SOURCE = _hermes_source()
if HERMES_SOURCE and HERMES_SOURCE not in sys.path:
    sys.path.insert(0, HERMES_SOURCE)


def pytest_report_header(config):
    try:
        import agent.subagent_lifecycle as lifecycle
        resolved = str(Path(lifecycle.__file__).resolve().parents[1])
    except Exception:
        resolved = "not importable (integration tests skip)"
    return f"hermes source: {HERMES_SOURCE or 'none'}; lifecycle API imported from: {resolved}"


def load_plugin():
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, PLUGIN_ROOT / "__init__.py", submodule_search_locations=[str(PLUGIN_ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = module
        spec.loader.exec_module(module)
    return sys.modules[PACKAGE]


# Pytest's package handling would otherwise import the hyphenated root as a bare ``__init__``.
sys.modules.setdefault("__init__", load_plugin())


@pytest.fixture(autouse=True)
def hermes_home(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    return home


class Clock:
    def __init__(self):
        self.now = 0.0
        self.on_sleep = []

    def monotonic(self):
        return self.now

    def wall(self):
        return 1_000_000.0 + self.now

    def sleep(self, seconds):
        self.now += seconds
        for callback in list(self.on_sleep):
            callback(self.now)


class FakeLifecycle:
    """Mirrors the public lifecycle API, including subagent_start firing inside launch()."""

    def __init__(self, service_getter):
        self.service_getter = service_getter
        self.requests = []
        self.state = {}
        self.results = {}
        self.cancels = []
        self.raise_on_launch = None
        self.fire_start_hook = True
        self.checks_during_launch = []
        self.batches = []  # request lists handed to launch_batch
        self.raise_on_batch = None

    def launch(self, request):
        if self.raise_on_launch is not None:
            raise self.raise_on_launch
        return self._start(request)

    def launch_batch(self, requests):
        """Hermes builds and starts the children in order; subagent_start fires for each before returning."""
        requests = list(requests)
        if self.raise_on_batch is not None:
            raise self.raise_on_batch
        self.batches.append(requests)
        return [self._start(request) for request in requests]

    def _start(self, request):
        subagent_id = f"sa-0-{len(self.requests):08x}"
        self.requests.append(request)
        if self.fire_start_hook:
            service = self.service_getter()
            service.on_subagent_start(child_subagent_id=subagent_id, child_session_id=f"child-{subagent_id}",
                                      child_role="leaf", child_goal=getattr(request, "goal", None))
            # What the child's first tool call would see before launch() even returns.
            self.checks_during_launch.append(service.pre_tool_call(tool_name="write_file", task_id=subagent_id))
        self.state[subagent_id] = "RUNNING"
        return SimpleNamespace(subagent_id=subagent_id, correlation_id=getattr(request, "correlation_id", None),
                               provider=getattr(request, "provider", None) or "lead-provider",
                               model=getattr(request, "model", None) or "lead-model")

    def finish(self, subagent_id, state="SUCCEEDED", summary="done", error=None, api_calls=3, duration=12.5):
        self.state[subagent_id] = state
        self.results[subagent_id] = SimpleNamespace(
            summary=summary, error_message=error, error_classification=None if state == "SUCCEEDED" else state,
            usage_metadata={"api_calls": api_calls}, tool_execution_summary={"duration_seconds": duration})

    def status(self, handle):
        return SimpleNamespace(state=self.state.get(handle.subagent_id, "UNKNOWN"))

    def result(self, handle):
        return self.results.get(handle.subagent_id)

    def cancel(self, handle, *, reason):
        self.cancels.append((handle.subagent_id, reason))
        return SimpleNamespace(accepted=True)


class FakeHost:
    def __init__(self, home):
        self.home_path = home
        self.settings = {}
        self.isolated = False
        self.parent_agent = SimpleNamespace(session_id="lead-1", provider=DIRECT, model="sonnet",
                                            enabled_toolsets=None, _delegate_depth=0)
        self.lifecycle = None
        self.fields = frozenset({"goal", "context", "role", "model", "allowed_toolsets", "correlation_id",
                                 "metadata", "blocked_tools", "working_directory", "parent_session_id",
                                 "timeout_seconds"})
        self.deadline = 420.0
        self.paused = False
        self.interrupt = False
        self.repo = home
        self.toolsets = {
            "read_file": "file", "search_files": "file", "write_file": "file", "patch": "file",
            "web_search": "web", "web_extract": "web", "x_search": "x_search", "vision_analyze": "vision",
            "session_search": "session_search", "skill_view": "skills", "skills_list": "skills",
            "todo_list": "todo", "terminal": "terminal", "process_manage": "terminal",
        }

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def home(self):
        return self.home_path

    def cwd(self):
        return self.repo

    def parent(self):
        return self.parent_agent

    def request_fields(self):
        return self.fields

    def make_request(self, **kwargs):
        return SimpleNamespace(**kwargs)

    def toolset_for_tool(self, name):
        return self.toolsets.get(name)

    def spawn_paused(self):
        return self.paused

    def tool_deadline(self):
        return self.deadline

    def interrupted(self):
        return self.interrupt

    def take_yield(self):
        return False

    def in_child_context(self):
        return False

    def supports_batch(self):
        return callable(getattr(self.lifecycle, "launch_batch", None))

    def supports_ui_card(self):
        return True


@pytest.fixture
def plugin():
    return load_plugin()


@pytest.fixture
def rig(plugin, hermes_home):
    from hermes_claude_agents.agents_service import AgentsService

    clock = Clock()
    host = FakeHost(hermes_home)
    holder = {}
    host.lifecycle = FakeLifecycle(lambda: holder["service"])
    service = AgentsService(host, clock=clock.monotonic, wall=clock.wall, sleep=clock.sleep)
    holder["service"] = service
    return SimpleNamespace(service=service, host=host, lifecycle=host.lifecycle, clock=clock, home=hermes_home)
