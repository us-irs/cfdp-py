from spacepackets.cfdp import ConditionCode
from cfdpy.mib import DefaultFaultHandlerBase
from cfdpy import TransactionId


class FaultHandler(DefaultFaultHandlerBase):
    def __init__(self):
        super().__init__()

    def notice_of_suspension_cb(
        self, transaction_id: TransactionId, cond: ConditionCode, progress: int
    ):
        pass

    def notice_of_cancellation_cb(
        self, transaction_id: TransactionId, cond: ConditionCode, progress: int
    ):
        pass

    def abandoned_cb(
        self, transaction_id: TransactionId, cond: ConditionCode, progress: int
    ):
        pass

    def ignore_cb(
        self, transaction_id: TransactionId, cond: ConditionCode, progress: int
    ):
        pass
