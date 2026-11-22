"""Immutable container names, independent of other instances in this process."""
from dataclasses import dataclass
import re
from .errors import CageError


@dataclass(frozen=True)
class Instance:
    name: str

    def __post_init__(self):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,47}', self.name):
            raise CageError('Instance must be a Docker name of 1–48 characters.')

    @property
    def network(self):
        return self.name + '-network'

    @property
    def browser(self):
        return self.name + '-browser'

    @property
    def claude(self):
        return self.name + '-claude'

    @property
    def check(self):
        return self.name + '-check'

    @property
    def names(self):
        return self.network, self.browser, self.claude, self.check

    @property
    def shutdown_order(self):
        return self.claude, self.browser, self.check, self.network
