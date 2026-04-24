from abc import ABC
from abc import abstractmethod


class CrmProvider(ABC):
    @abstractmethod
    def push_meeting_summary(self, meeting_id: str, summary: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def push_tasks(self, meeting_id: str) -> None:
        raise NotImplementedError


class CrmStubProvider(CrmProvider):
    def push_meeting_summary(self, meeting_id: str, summary: str) -> None:
        _ = (meeting_id, summary)

    def push_tasks(self, meeting_id: str) -> None:
        _ = meeting_id
