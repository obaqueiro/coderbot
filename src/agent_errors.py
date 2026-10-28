"""Typed agent failures retain subprocess diagnostics for incident attachments."""
class AgentContentFilterError(RuntimeError):
    """The provider rejected the turn; retrying unchanged input is not useful."""
    def __init__(self, message, proc, *, request="", partial_response="", provider_detail=""):
        super().__init__(message)
        self.cmd = proc.args
        self.returncode = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.request = request
        self.partial_response = partial_response
        self.provider_detail = provider_detail


class AgentTransportError(ConnectionError):
    def __init__(self, message, proc):
        super().__init__(message)
        self.cmd = proc.args
        self.returncode = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
