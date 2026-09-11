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


class BrokerDefinitelyNotSent(BrokerError):
    """Transport was positively not attempted; explicit retry may be prepared."""
    retryable = True


class BrokerCommandOutcomeUnknown(BrokerError):
    """A network command may have reached the broker; reconciliation is mandatory."""
    safety_critical = True
