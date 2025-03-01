import abc
import binascii
from collections import defaultdict
from json import loads
import logging
import os
import sys
import threading
from typing import Dict
from typing import List
from typing import Optional
from typing import TYPE_CHECKING
from typing import TextIO

import six
import tenacity

import ddtrace
from ddtrace import config
from ddtrace.appsec._remoteconfiguration import enable_appsec_rc
from ddtrace.vendor.dogstatsd import DogStatsd

from .. import agent
from .. import compat
from .. import periodic
from .. import service
from ...constants import KEEP_SPANS_RATE_KEY
from ...internal.telemetry import telemetry_metrics_writer
from ...internal.telemetry import telemetry_writer
from ...internal.utils.formats import asbool
from ...internal.utils.formats import parse_tags_str
from ...internal.utils.time import StopWatch
from ...sampler import BasePrioritySampler
from ...sampler import BaseSampler
from .._encoding import BufferFull
from .._encoding import BufferItemTooLarge
from ..agent import get_connection
from ..encoding import JSONEncoderV2
from ..logger import get_logger
from ..runtime import container
from ..sma import SimpleMovingAverage
from .writer_client import AgentWriterClientV3
from .writer_client import AgentWriterClientV4
from .writer_client import WRITER_CLIENTS
from .writer_client import WriterClientBase


if TYPE_CHECKING:  # pragma: no cover
    from ddtrace import Span

    from .agent import ConnectionType


log = get_logger(__name__)

LOG_ERR_INTERVAL = 60

# The window size should be chosen so that the look-back period is
# greater-equal to the agent API's timeout. Although most tracers have a
# 2s timeout, the java tracer has a 10s timeout, so we set the window size
# to 10 buckets of 1s duration.
DEFAULT_SMA_WINDOW = 10

DEFAULT_BUFFER_SIZE = 8 << 20  # 8 MB
DEFAULT_MAX_PAYLOAD_SIZE = 8 << 20  # 8 MB
DEFAULT_PROCESSING_INTERVAL = 1.0
DEFAULT_REUSE_CONNECTIONS = False


def get_writer_buffer_size():
    # type: () -> int
    return int(os.getenv("DD_TRACE_WRITER_BUFFER_SIZE_BYTES", default=DEFAULT_BUFFER_SIZE))


def get_writer_max_payload_size():
    # type: () -> int
    return int(os.getenv("DD_TRACE_WRITER_MAX_PAYLOAD_SIZE_BYTES", default=DEFAULT_MAX_PAYLOAD_SIZE))


def get_writer_interval_seconds():
    # type: () -> float
    return float(os.getenv("DD_TRACE_WRITER_INTERVAL_SECONDS", default=DEFAULT_PROCESSING_INTERVAL))


def get_writer_reuse_connections():
    # type: () -> bool
    return asbool(os.getenv("DD_TRACE_WRITER_REUSE_CONNECTIONS", DEFAULT_REUSE_CONNECTIONS))


def _human_size(nbytes):
    """Return a human-readable size."""
    i = 0
    suffixes = ["B", "KB", "MB", "GB", "TB"]
    while nbytes >= 1000 and i < len(suffixes) - 1:
        nbytes /= 1000.0
        i += 1
    f = ("%.2f" % nbytes).rstrip("0").rstrip(".")
    return "%s%s" % (f, suffixes[i])


class Response(object):
    """
    Custom API Response object to represent a response from calling the API.

    We do this to ensure we know expected properties will exist, and so we
    can call `resp.read()` and load the body once into an instance before we
    close the HTTPConnection used for the request.
    """

    __slots__ = ["status", "body", "reason", "msg"]

    def __init__(self, status=None, body=None, reason=None, msg=None):
        self.status = status
        self.body = body
        self.reason = reason
        self.msg = msg

    @classmethod
    def from_http_response(cls, resp):
        """
        Build a ``Response`` from the provided ``HTTPResponse`` object.

        This function will call `.read()` to consume the body of the ``HTTPResponse`` object.

        :param resp: ``HTTPResponse`` object to build the ``Response`` from
        :type resp: ``HTTPResponse``
        :rtype: ``Response``
        :returns: A new ``Response``
        """
        return cls(
            status=resp.status,
            body=resp.read(),
            reason=getattr(resp, "reason", None),
            msg=getattr(resp, "msg", None),
        )

    def get_json(self):
        """Helper to parse the body of this request as JSON"""
        try:
            body = self.body
            if not body:
                log.debug("Empty reply from Datadog Agent, %r", self)
                return

            if not isinstance(body, str) and hasattr(body, "decode"):
                body = body.decode("utf-8")

            if hasattr(body, "startswith") and body.startswith("OK"):
                # This typically happens when using a priority-sampling enabled
                # library with an outdated agent. It still works, but priority sampling
                # will probably send too many traces, so the next step is to upgrade agent.
                log.debug(
                    "Cannot parse Datadog Agent response. "
                    "This occurs because Datadog agent is out of date or DATADOG_PRIORITY_SAMPLING=false is set"
                )
                return

            return loads(body)
        except (ValueError, TypeError):
            log.debug("Unable to parse Datadog Agent JSON response: %r", body, exc_info=True)

    def __repr__(self):
        return "{0}(status={1!r}, body={2!r}, reason={3!r}, msg={4!r})".format(
            self.__class__.__name__,
            self.status,
            self.body,
            self.reason,
            self.msg,
        )


class TraceWriter(six.with_metaclass(abc.ABCMeta)):
    @abc.abstractmethod
    def recreate(self):
        # type: () -> TraceWriter
        pass

    @abc.abstractmethod
    def stop(self, timeout=None):
        # type: (Optional[float]) -> None
        pass

    @abc.abstractmethod
    def write(self, spans=None):
        # type: (Optional[List[Span]]) -> None
        pass

    @abc.abstractmethod
    def flush_queue(self):
        # type: () -> None
        pass


class LogWriter(TraceWriter):
    def __init__(
        self,
        out=sys.stdout,  # type: TextIO
        sampler=None,  # type: Optional[BaseSampler]
        priority_sampler=None,  # type: Optional[BasePrioritySampler]
    ):
        # type: (...) -> None
        self._sampler = sampler
        self._priority_sampler = priority_sampler
        self.encoder = JSONEncoderV2()
        self.out = out

    def recreate(self):
        # type: () -> LogWriter
        """Create a new instance of :class:`LogWriter` using the same settings from this instance

        :rtype: :class:`LogWriter`
        :returns: A new :class:`LogWriter` instance
        """
        writer = self.__class__(out=self.out, sampler=self._sampler, priority_sampler=self._priority_sampler)
        return writer

    def stop(self, timeout=None):
        # type: (Optional[float]) -> None
        return

    def write(self, spans=None):
        # type: (Optional[List[Span]]) -> None
        if not spans:
            return

        encoded = self.encoder.encode_traces([spans])
        self.out.write(encoded + "\n")
        self.out.flush()

    def flush_queue(self):
        # type: () -> None
        pass


class HTTPWriter(periodic.PeriodicService, TraceWriter):
    """Writer to an arbitrary HTTP intake endpoint."""

    RETRY_ATTEMPTS = 3
    HTTP_METHOD = "PUT"
    STATSD_NAMESPACE = "tracer"

    def __init__(
        self,
        intake_url,  # type: str
        clients,  # type: List[WriterClientBase]
        sampler=None,  # type: Optional[BaseSampler]
        priority_sampler=None,  # type: Optional[BasePrioritySampler]
        processing_interval=get_writer_interval_seconds(),  # type: float
        # Match the payload size since there is no functionality
        # to flush dynamically.
        buffer_size=None,  # type: Optional[int]
        max_payload_size=None,  # type: Optional[int]
        timeout=agent.get_trace_agent_timeout(),  # type: float
        dogstatsd=None,  # type: Optional[DogStatsd]
        sync_mode=False,  # type: bool
        reuse_connections=None,  # type: Optional[bool]
        headers=None,  # type: Optional[Dict[str, str]]
    ):
        # type: (...) -> None

        super(HTTPWriter, self).__init__(interval=processing_interval)
        self.intake_url = intake_url
        self._buffer_size = buffer_size
        self._max_payload_size = max_payload_size
        self._sampler = sampler
        self._priority_sampler = priority_sampler
        self._headers = headers or {}
        self._timeout = timeout

        self._clients = clients
        self.dogstatsd = dogstatsd
        self._metrics_reset()
        self._drop_sma = SimpleMovingAverage(DEFAULT_SMA_WINDOW)
        self._sync_mode = sync_mode
        self._conn = None  # type: Optional[ConnectionType]
        # The connection has to be locked since there exists a race between
        # the periodic thread of HTTPWriter and other threads that might
        # force a flush with `flush_queue()`.
        self._conn_lck = threading.RLock()  # type: threading.RLock
        self._retry_upload = tenacity.Retrying(
            # Retry RETRY_ATTEMPTS times within the first half of the processing
            # interval, using a Fibonacci policy with jitter
            wait=tenacity.wait_random_exponential(
                multiplier=(0.618 * self.interval / (1.618 ** self.RETRY_ATTEMPTS) / 2),
                exp_base=1.618,
            ),
            stop=tenacity.stop_after_attempt(self.RETRY_ATTEMPTS),
            retry=tenacity.retry_if_exception_type((compat.httplib.HTTPException, OSError, IOError)),
        )
        self._log_error_payloads = asbool(os.environ.get("_DD_TRACE_WRITER_LOG_ERROR_PAYLOADS", False))
        self._reuse_connections = get_writer_reuse_connections() if reuse_connections is None else reuse_connections

    @property
    def _intake_endpoint(self):
        return "{}/{}".format(self.intake_url, self._endpoint)

    @property
    def _endpoint(self):
        return self._clients[0].ENDPOINT

    @property
    def _encoder(self):
        return self._clients[0].encoder

    def _metrics_dist(self, name, count=1, tags=None):
        self._metrics[name]["count"] += count
        if tags:
            self._metrics[name]["tags"].extend(tags)

    def _metrics_reset(self):
        self._metrics = defaultdict(lambda: {"count": 0, "tags": []})

    def _set_drop_rate(self):
        dropped = sum(
            self._metrics[metric]["count"]
            for metric in ("encoder.dropped.traces", "buffer.dropped.traces", "http.dropped.traces")
        )
        accepted = self._metrics["writer.accepted.traces"]["count"]

        if dropped > accepted:
            # Sanity check, we cannot drop more traces than we accepted.
            log.debug(
                "dropped.traces metric is greater than accepted.traces metric"
                "This difference may be reconciled in future metric uploads (dropped.traces: %d, accepted.traces: %d)",
                dropped,
                accepted,
            )
            accepted = dropped

        self._drop_sma.set(dropped, accepted)

    def _set_keep_rate(self, trace):
        if trace:
            trace[0].set_metric(KEEP_SPANS_RATE_KEY, 1.0 - self._drop_sma.get())

    def _reset_connection(self):
        # type: () -> None
        with self._conn_lck:
            if self._conn:
                self._conn.close()
                self._conn = None

    def _put(self, data, headers, client):
        # type: (bytes, Dict[str, str], WriterClientBase) -> Response
        sw = StopWatch()
        sw.start()
        with self._conn_lck:
            if self._conn is None:
                log.debug("creating new intake connection to %s with timeout %d", self.intake_url, self._timeout)
                self._conn = get_connection(self.intake_url, self._timeout)
            try:
                log.debug("Sending request: %s %s %s %s", self.HTTP_METHOD, client.ENDPOINT, data, headers)
                self._conn.request(
                    self.HTTP_METHOD,
                    client.ENDPOINT,
                    data,
                    headers,
                )
                resp = compat.get_connection_response(self._conn)
                log.debug("Got response: %s %s", resp.status, resp.reason)
                t = sw.elapsed()
                if t >= self.interval:
                    log_level = logging.WARNING
                else:
                    log_level = logging.DEBUG
                log.log(log_level, "sent %s in %.5fs to %s", _human_size(len(data)), t, self._intake_endpoint)
            except Exception:
                # Always reset the connection when an exception occurs
                self._reset_connection()
                raise
            else:
                return Response.from_http_response(resp)
            finally:
                # Reset the connection if reusing connections is disabled.
                if not self._reuse_connections:
                    self._reset_connection()

    def _get_finalized_headers(self, count):
        return self._headers.copy()

    def _send_payload(self, payload, count, client):
        headers = self._get_finalized_headers(count)

        self._metrics_dist("http.requests")

        response = self._put(payload, headers, client)

        if response.status >= 400:
            self._metrics_dist("http.errors", tags=["type:%s" % response.status])
        else:
            self._metrics_dist("http.sent.bytes", len(payload))

        if response.status not in (404, 415) and response.status >= 400:
            msg = "failed to send traces to intake at %s: HTTP error status %s, reason %s"
            log_args = (
                self._intake_endpoint,
                response.status,
                response.reason,
            )
            # Append the payload if requested
            if self._log_error_payloads:
                msg += ", payload %s"
                # If the payload is bytes then hex encode the value before logging
                if isinstance(payload, six.binary_type):
                    log_args += (binascii.hexlify(payload).decode(),)
                else:
                    log_args += (payload,)

            log.error(msg, *log_args)
            self._metrics_dist("http.dropped.bytes", len(payload))
            self._metrics_dist("http.dropped.traces", count)
        return response

    def write(self, spans=None):
        for client in self._clients:
            self._write_with_client(client, spans=spans)
        if self._sync_mode:
            self.flush_queue()

    def _write_with_client(self, client, spans=None):
        # type: (WriterClientBase, Optional[List[Span]]) -> None
        if spans is None:
            return

        if self._sync_mode is False:
            # Start the HTTPWriter on first write.
            try:
                if self.status != service.ServiceStatus.RUNNING:
                    self.start()

            except service.ServiceStatusError:
                pass

        self._metrics_dist("writer.accepted.traces")
        self._set_keep_rate(spans)

        try:
            client.encoder.put(spans)
        except BufferItemTooLarge as e:
            payload_size = e.args[0]
            log.warning(
                "trace (%db) larger than payload buffer item limit (%db), dropping",
                payload_size,
                client.encoder.max_item_size,
            )
            self._metrics_dist("buffer.dropped.traces", 1, tags=["reason:t_too_big"])
            self._metrics_dist("buffer.dropped.bytes", payload_size, tags=["reason:t_too_big"])
        except BufferFull as e:
            payload_size = e.args[0]
            log.warning(
                "trace buffer (%s traces %db/%db) cannot fit trace of size %db, dropping (writer status: %s)",
                len(client.encoder),
                client.encoder.size,
                client.encoder.max_size,
                payload_size,
                self.status.value,
            )
            self._metrics_dist("buffer.dropped.traces", 1, tags=["reason:full"])
            self._metrics_dist("buffer.dropped.bytes", payload_size, tags=["reason:full"])
        else:
            self._metrics_dist("buffer.accepted.traces", 1)
            self._metrics_dist("buffer.accepted.spans", len(spans))

    def flush_queue(self, raise_exc=False):
        try:
            for client in self._clients:
                self._flush_queue_with_client(client, raise_exc=raise_exc)
        finally:
            self._set_drop_rate()
            self._metrics_reset()

    def _flush_queue_with_client(self, client, raise_exc=False):
        # type: (WriterClientBase, bool) -> None
        n_traces = len(client.encoder)
        try:
            encoded = client.encoder.encode()
            if encoded is None:
                return
        except Exception:
            log.error("failed to encode trace with encoder %r", client.encoder, exc_info=True)
            self._metrics_dist("encoder.dropped.traces", n_traces)
            return

        try:
            self._retry_upload(self._send_payload, encoded, n_traces, client)
        except tenacity.RetryError as e:
            self._metrics_dist("http.errors", tags=["type:err"])
            self._metrics_dist("http.dropped.bytes", len(encoded))
            self._metrics_dist("http.dropped.traces", n_traces)
            if raise_exc:
                e.reraise()
            else:
                log.error(
                    "failed to send, dropping %d traces to intake at %s after %d retries (%s)",
                    n_traces,
                    self._intake_endpoint,
                    e.last_attempt.attempt_number,
                    e.last_attempt.exception(),
                )
        finally:
            if config.health_metrics_enabled and self.dogstatsd:
                namespace = self.STATSD_NAMESPACE
                # Note that we cannot use the batching functionality of dogstatsd because
                # it's not thread-safe.
                # https://github.com/DataDog/datadogpy/issues/439
                # This really isn't ideal as now we're going to do a ton of socket calls.
                self.dogstatsd.distribution("datadog.%s.http.sent.bytes" % namespace, len(encoded))
                self.dogstatsd.distribution("datadog.%s.http.sent.traces" % namespace, n_traces)
                for name, metric in self._metrics.items():
                    self.dogstatsd.distribution(
                        "datadog.%s.%s" % (namespace, name), metric["count"], tags=metric["tags"]
                    )

    def periodic(self):
        self.flush_queue(raise_exc=False)

    def _stop_service(
        self,
        timeout=None,  # type: Optional[float]
    ):
        # type: (...) -> None
        # FIXME: don't join() on stop(), let the caller handle this
        super(HTTPWriter, self)._stop_service()
        self.join(timeout=timeout)

    def on_shutdown(self):
        try:
            self.periodic()
        finally:
            self._reset_connection()


class AgentWriter(HTTPWriter):
    """
    The Datadog Agent supports (at the time of writing this) receiving trace
    payloads up to 50MB. A trace payload is just a list of traces and the agent
    expects a trace to be complete. That is, all spans with the same trace_id
    should be in the same trace.
    """

    RETRY_ATTEMPTS = 3
    HTTP_METHOD = "PUT"
    STATSD_NAMESPACE = "tracer"

    def __init__(
        self,
        agent_url,  # type: str
        sampler=None,  # type: Optional[BaseSampler]
        priority_sampler=None,  # type: Optional[BasePrioritySampler]
        processing_interval=get_writer_interval_seconds(),  # type: float
        # Match the payload size since there is no functionality
        # to flush dynamically.
        buffer_size=None,  # type: Optional[int]
        max_payload_size=None,  # type: Optional[int]
        timeout=agent.get_trace_agent_timeout(),  # type: float
        dogstatsd=None,  # type: Optional[DogStatsd]
        report_metrics=False,  # type: bool
        sync_mode=False,  # type: bool
        api_version=None,  # type: Optional[str]
        reuse_connections=None,  # type: Optional[bool]
        headers=None,  # type: Optional[Dict[str, str]]
    ):
        # type: (...) -> None
        if buffer_size is not None and buffer_size <= 0:
            raise ValueError("Writer buffer size must be positive")
        if max_payload_size is not None and max_payload_size <= 0:
            raise ValueError("Max payload size must be positive")
        # Default to v0.4 if we are on Windows since there is a known compatibility issue
        # https://github.com/DataDog/dd-trace-py/issues/4829
        # DEV: sys.platform on windows should be `win32` or `cygwin`, but using `startswith`
        #      as a safety precaution.
        #      https://docs.python.org/3/library/sys.html#sys.platform
        is_windows = sys.platform.startswith("win") or sys.platform.startswith("cygwin")
        default_api_version = "v0.4" if is_windows else "v0.5"

        self._api_version = (
            api_version
            or os.getenv("DD_TRACE_API_VERSION")
            or (default_api_version if priority_sampler is not None else "v0.3")
        )
        if is_windows and self._api_version == "v0.5":
            raise RuntimeError(
                "There is a known compatibility issue with v0.5 API and Windows, "
                "please see https://github.com/DataDog/dd-trace-py/issues/4829 for more details."
            )

        buffer_size = buffer_size or get_writer_buffer_size()
        max_payload_size = max_payload_size or get_writer_max_payload_size()
        try:
            client = WRITER_CLIENTS[self._api_version](buffer_size, max_payload_size)
        except KeyError:
            raise ValueError(
                "Unsupported api version: '%s'. The supported versions are: %r"
                % (self._api_version, ", ".join(sorted(WRITER_CLIENTS.keys())))
            )

        _headers = {
            "Datadog-Meta-Lang": "python",
            "Datadog-Meta-Lang-Version": compat.PYTHON_VERSION,
            "Datadog-Meta-Lang-Interpreter": compat.PYTHON_INTERPRETER,
            "Datadog-Meta-Tracer-Version": ddtrace.__version__,
            "Datadog-Client-Computed-Top-Level": "yes",
        }
        if headers:
            _headers.update(headers)
        self._container_info = container.get_container_info()
        if self._container_info and self._container_info.container_id:
            _headers.update(
                {
                    "Datadog-Container-Id": self._container_info.container_id,
                }
            )

        _headers.update({"Content-Type": client.encoder.content_type})  # type: ignore[attr-defined]
        additional_header_str = os.environ.get("_DD_TRACE_WRITER_ADDITIONAL_HEADERS")
        if additional_header_str is not None:
            _headers.update(parse_tags_str(additional_header_str))
        super(AgentWriter, self).__init__(
            intake_url=agent_url,
            clients=[client],
            sampler=sampler,
            priority_sampler=priority_sampler,
            processing_interval=processing_interval,
            buffer_size=buffer_size,
            max_payload_size=max_payload_size,
            timeout=timeout,
            dogstatsd=dogstatsd,
            sync_mode=sync_mode,
            reuse_connections=reuse_connections,
            headers=_headers,
        )

    def recreate(self):
        # type: () -> HTTPWriter
        return self.__class__(
            agent_url=self.agent_url,
            sampler=self._sampler,
            priority_sampler=self._priority_sampler,
            processing_interval=self._interval,
            buffer_size=self._buffer_size,
            max_payload_size=self._max_payload_size,
            timeout=self._timeout,
            dogstatsd=self.dogstatsd,
            sync_mode=self._sync_mode,
            api_version=self._api_version,
        )

    @property
    def agent_url(self):
        return self.intake_url

    @property
    def _agent_endpoint(self):
        return self._intake_endpoint

    def _downgrade(self, payload, response, client):
        if client.ENDPOINT == "v0.5/traces":
            self._clients = [AgentWriterClientV4(self._buffer_size, self._max_payload_size)]
            # Since we have to change the encoding in this case, the payload
            # would need to be converted to the downgraded encoding before
            # sending it, but we chuck it away instead.
            log.warning(
                "Dropping trace payload due to the downgrade to an incompatible API version (from v0.5 to v0.4). To "
                "avoid this from happening in the future, either ensure that the Datadog agent has a v0.5/traces "
                "endpoint available, or explicitly set the trace API version to, e.g., v0.4."
            )
            return None
        if client.ENDPOINT == "v0.4/traces":
            self._clients = [AgentWriterClientV3(self._buffer_size, self._max_payload_size)]
            # These endpoints share the same encoding, so we can try sending the
            # same payload over the downgraded endpoint.
            return payload
        raise ValueError()

    def _send_payload(self, payload, count, client):
        response = super(AgentWriter, self)._send_payload(payload, count, client)
        if response.status in [404, 415]:
            log.debug("calling endpoint '%s' but received %s; downgrading API", client.ENDPOINT, response.status)
            try:
                payload = self._downgrade(payload, response, client)
            except ValueError:
                log.error(
                    "unsupported endpoint '%s': received response %s from intake (%s)",
                    client.ENDPOINT,
                    response.status,
                    self.intake_url,
                )
            else:
                if payload is not None:
                    self._send_payload(payload, count, client)
        elif response.status < 400 and (self._priority_sampler or isinstance(self._sampler, BasePrioritySampler)):
            result_traces_json = response.get_json()
            if result_traces_json and "rate_by_service" in result_traces_json:
                try:
                    if self._priority_sampler:
                        self._priority_sampler.update_rate_by_service_sample_rates(
                            result_traces_json["rate_by_service"],
                        )
                    if isinstance(self._sampler, BasePrioritySampler):
                        self._sampler.update_rate_by_service_sample_rates(
                            result_traces_json["rate_by_service"],
                        )
                except ValueError:
                    log.error("sample_rate is negative, cannot update the rate samplers")

    def start(self):
        super(AgentWriter, self).start()
        try:
            # instrumentation telemetry writer should be enabled/started after the global tracer and configs
            # are initialized
            if asbool(os.getenv("DD_INSTRUMENTATION_TELEMETRY_ENABLED", True)):
                telemetry_writer.enable()

            if config._telemetry_metrics_enabled:
                telemetry_metrics_writer.enable()

            # appsec remote config should be enabled/started after the global tracer and configs
            # are initialized
            enable_appsec_rc()
        except service.ServiceStatusError:
            pass

    def _get_finalized_headers(self, count):
        headers = self._headers.copy()
        headers["X-Datadog-Trace-Count"] = str(count)
        return headers
