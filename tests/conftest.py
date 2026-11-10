import pytest

from autopilot.config import Settings
from autopilot.schema import JobInput
from autopilot.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        api_token="a" * 48,
        worker_token="w" * 48,
        local_model="fixture-local",
        cloud_model="fixture-cloud",
        openai_key="test-key",
        input_usd_per_million=1,
        output_usd_per_million=4,
        headless=True,
        economy_enabled=False,
    )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state.sqlite")


@pytest.fixture
def brief():
    return JobInput(
        prompt="Create a tested Python file inside this task workspace and read the result back. " * 4
    )
