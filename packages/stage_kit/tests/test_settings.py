import pytest
from pydantic import ValidationError
from stage_kit import Profile, StageSettings

ENV = {
    "STAGE_NAME": "fraud",
    "RABBITMQ_URL": "amqp://flowforge:flowforge@rabbitmq:5672/",
    "STAGE_LATENCY_MIN_S": "0.1",
    "STAGE_LATENCY_MAX_S": "0.5",
    "STAGE_TRANSIENT_PROBABILITY": "0.2",
    "STAGE_REJECT_PROBABILITY": "0.1",
}


@pytest.fixture
def configure(monkeypatch):
    def apply(**changes):
        for key, value in (ENV | changes).items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)

    return apply


def test_the_environment_defines_the_stage_and_its_profile(configure):
    configure(STAGE_SEED="7")

    settings = StageSettings()

    assert settings.name == "fraud"
    assert settings.seed == 7
    assert settings.profile == Profile(0.1, 0.5, 0.2, 0.1)


@pytest.mark.parametrize(
    "change",
    [
        {"STAGE_NAME": None},
        {"STAGE_NAME": ""},
        {"STAGE_NAME": "Gift-Wrap"},
        {"STAGE_NAME": "../fraud"},
        {"RABBITMQ_URL": ""},
        {"STAGE_LATENCY_MIN_S": "2"},
        {"STAGE_TRANSIENT_PROBABILITY": "0.8", "STAGE_REJECT_PROBABILITY": "0.3"},
        {"STAGE_REJECT_PROBABILITY": "1.5"},
        {"STAGE_LATENCY_MIN_S": None},
    ],
)
def test_an_invalid_environment_fails_startup(configure, change):
    configure(**change)

    with pytest.raises(ValidationError):
        StageSettings()
