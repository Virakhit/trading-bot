class BrokerError(RuntimeError):
    retryable = False
    safety_critical = False


class BrokerUnavailable(BrokerError):
    retryable = True


class BrokerAuthenticationError(BrokerError):
    pass


class UnsafeEnvironmentError(BrokerError):
    safety_critical = True


class OrderRejected(BrokerError):
    pass


class InvalidOrderTransition(BrokerError):
    pass


class ReconciliationMismatch(BrokerError):
    pass


class DuplicateBrokerEvent(BrokerError):
    pass


class StaleMarketData(BrokerError):
    pass


class UnsupportedBrokerFeature(BrokerError):
    pass


class BrokerEventConsistencyError(BrokerError):
    safety_critical = True


class SubmissionOutcomeUnknown(BrokerError):
    safety_critical = True

