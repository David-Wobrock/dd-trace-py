import functools
import json

import flask
from six import BytesIO
import werkzeug
from werkzeug.exceptions import BadRequest
from werkzeug.exceptions import abort
import xmltodict

from ddtrace.appsec.iast._patch import if_iast_taint_returned_object_for
from ddtrace.appsec.iast._util import _is_iast_enabled
from ddtrace.constants import SPAN_KIND
from ddtrace.ext import SpanKind
from ddtrace.internal.constants import COMPONENT

from ...appsec import _asm_request_context
from ...appsec import utils
from ...internal import _context


# Not all versions of flask/werkzeug have this mixin
try:
    from werkzeug.wrappers.json import JSONMixin

    _HAS_JSON_MIXIN = True
except ImportError:
    _HAS_JSON_MIXIN = False

from ddtrace import Pin
from ddtrace import config
from ddtrace.vendor.wrapt import wrap_function_wrapper as _w

from .. import trace_utils
from ...constants import ANALYTICS_SAMPLE_RATE_KEY
from ...constants import SPAN_MEASURED_KEY
from ...contrib.wsgi.wsgi import _DDWSGIMiddlewareBase
from ...ext import SpanTypes
from ...ext import http
from ...internal.compat import maybe_stringify
from ...internal.logger import get_logger
from ...internal.utils import get_argument_value
from ...internal.utils.version import parse_version
from ..trace_utils import _get_request_header_user_agent
from ..trace_utils import _set_url_tag
from ..trace_utils import unwrap as _u
from .helpers import get_current_app
from .helpers import simple_tracer
from .helpers import with_instance_pin
from .wrappers import wrap_function
from .wrappers import wrap_signal
from .wrappers import wrap_view


try:
    from json import JSONDecodeError
except ImportError:
    # handling python 2.X import error
    JSONDecodeError = ValueError  # type: ignore


log = get_logger(__name__)

FLASK_ENDPOINT = "flask.endpoint"
FLASK_VIEW_ARGS = "flask.view_args"
FLASK_URL_RULE = "flask.url_rule"
FLASK_VERSION = "flask.version"
_BODY_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

# Configure default configuration
config._add(
    "flask",
    dict(
        # Flask service configuration
        _default_service="flask",
        collect_view_args=True,
        distributed_tracing_enabled=True,
        template_default_name="<memory>",
        trace_signals=True,
    ),
)


if _HAS_JSON_MIXIN:

    class RequestWithJson(werkzeug.Request, JSONMixin):
        pass

    _RequestType = RequestWithJson
else:
    _RequestType = werkzeug.Request

# Extract flask version into a tuple e.g. (0, 12, 1) or (1, 0, 2)
# DEV: This makes it so we can do `if flask_version >= (0, 12, 0):`
# DEV: Example tests:
#      (0, 10, 0) > (0, 10)
#      (0, 10, 0) >= (0, 10, 0)
#      (0, 10, 1) >= (0, 10)
#      (0, 11, 1) >= (0, 10)
#      (0, 11, 1) >= (0, 10, 2)
#      (1, 0, 0) >= (0, 10)
#      (0, 9) == (0, 9)
#      (0, 9, 0) != (0, 9)
#      (0, 8, 5) <= (0, 9)
flask_version_str = getattr(flask, "__version__", "0.0.0")
flask_version = parse_version(flask_version_str)


def taint_request_init(wrapped, instance, args, kwargs):
    wrapped(*args, **kwargs)
    if _is_iast_enabled():
        try:
            from ddtrace.appsec.iast._input_info import Input_info
            from ddtrace.appsec.iast._taint_tracking import taint_pyobject

            taint_pyobject(
                instance.query_string,
                Input_info("http.request.querystring", instance.query_string, "http.request.querystring"),
            )
            taint_pyobject(instance.path, Input_info("http.request.path", instance.path, "http.request.path"))
        except Exception:
            log.debug("Unexpected exception while tainting pyobject", exc_info=True)


class _FlaskWSGIMiddleware(_DDWSGIMiddlewareBase):
    _request_span_name = "flask.request"
    _application_span_name = "flask.application"
    _response_span_name = "flask.response"

    def _traced_start_response(self, start_response, req_span, app_span, status_code, headers, exc_info=None):
        code, _, _ = status_code.partition(" ")
        # If values are accessible, set the resource as `<method> <path>` and add other request tags
        _set_request_tags(req_span)

        # Override root span resource name to be `<method> 404` for 404 requests
        # DEV: We do this because we want to make it easier to see all unknown requests together
        #      Also, we do this to reduce the cardinality on unknown urls
        # DEV: If we have an endpoint or url rule tag, then we don't need to do this,
        #      we still want `GET /product/<int:product_id>` grouped together,
        #      even if it is a 404
        if not req_span.get_tag(FLASK_ENDPOINT) and not req_span.get_tag(FLASK_URL_RULE):
            req_span.resource = " ".join((flask.request.method, code))

        trace_utils.set_http_meta(
            req_span, config.flask, status_code=code, response_headers=headers, route=req_span.get_tag(FLASK_URL_RULE)
        )

        if config._appsec_enabled and not _context.get_item("http.request.blocked", span=req_span):
            log.debug("Flask WAF call for Suspicious Request Blocking on response")
            _asm_request_context.call_waf_callback()
            if _context.get_item("http.request.blocked", span=req_span):
                # response code must be set here, or it will be too late
                ctype = (
                    "text/html"
                    if "text/html" in _asm_request_context.get_headers().get("Accept", "").lower()
                    else "text/json"
                )
                response_headers = [("content-type", ctype)]
                result = start_response("403 FORBIDDEN", response_headers)
                trace_utils.set_http_meta(req_span, config.flask, status_code="403", response_headers=response_headers)
            else:
                result = start_response(status_code, headers)
        else:
            result = start_response(status_code, headers)
        return result

    def _request_span_modifier(self, span, environ, parsed_headers=None):
        # Create a werkzeug request from the `environ` to make interacting with it easier
        # DEV: This executes before a request context is created
        request = _RequestType(environ)

        # Default resource is method and path:
        #   GET /
        #   POST /save
        # We will override this below in `traced_dispatch_request` when we have a `
        # RequestContext` and possibly a url rule
        span.resource = " ".join((request.method, request.path))

        span.set_tag(SPAN_MEASURED_KEY)
        # set analytics sample rate with global config enabled
        sample_rate = config.flask.get_analytics_sample_rate(use_global_config=True)
        if sample_rate is not None:
            span.set_tag(ANALYTICS_SAMPLE_RATE_KEY, sample_rate)

        span.set_tag_str(FLASK_VERSION, flask_version_str)

        req_body = None
        if config._appsec_enabled and request.method in _BODY_METHODS:
            content_type = request.content_type
            wsgi_input = environ.get("wsgi.input", "")

            # Copy wsgi input if not seekable
            if wsgi_input:
                try:
                    seekable = wsgi_input.seekable()
                except AttributeError:
                    seekable = False
                if not seekable:
                    content_length = int(environ.get("CONTENT_LENGTH", 0))
                    body = wsgi_input.read(content_length) if content_length else wsgi_input.read()
                    environ["wsgi.input"] = BytesIO(body)

            try:
                if content_type == "application/json" or content_type == "text/json":
                    if _HAS_JSON_MIXIN and hasattr(request, "json") and request.json:
                        req_body = request.json
                    else:
                        req_body = json.loads(request.data.decode("UTF-8"))
                elif content_type in ("application/xml", "text/xml"):
                    req_body = xmltodict.parse(request.get_data())
                elif hasattr(request, "form"):
                    req_body = request.form.to_dict()
                else:
                    # no raw body
                    req_body = None
            except (
                AttributeError,
                RuntimeError,
                TypeError,
                BadRequest,
                ValueError,
                JSONDecodeError,
                xmltodict.expat.ExpatError,
                xmltodict.ParsingInterrupted,
            ):
                log.warning("Failed to parse werkzeug request body", exc_info=True)
            finally:
                # Reset wsgi input to the beginning
                if wsgi_input:
                    if seekable:
                        wsgi_input.seek(0)
                    else:
                        environ["wsgi.input"] = BytesIO(body)

        trace_utils.set_http_meta(
            span,
            config.flask,
            method=request.method,
            url=request.base_url,
            raw_uri=request.url,
            query=request.query_string,
            parsed_query=request.args,
            request_headers=request.headers,
            request_cookies=request.cookies,
            request_body=req_body,
            peer_ip=request.remote_addr,
        )


def patch():
    """
    Patch `flask` module for tracing
    """
    # Check to see if we have patched Flask yet or not
    if getattr(flask, "_datadog_patch", False):
        return
    setattr(flask, "_datadog_patch", True)

    Pin().onto(flask.Flask)

    # IAST
    _w(
        "werkzeug.datastructures",
        "EnvironHeaders.__getitem__",
        functools.partial(if_iast_taint_returned_object_for, "http.request.header"),
    )
    _w(
        "werkzeug.datastructures",
        "ImmutableMultiDict.__getitem__",
        functools.partial(if_iast_taint_returned_object_for, "http.request.parameter"),
    )
    _w("werkzeug.wrappers.request", "Request.__init__", taint_request_init)
    _w(
        "werkzeug.wrappers.request",
        "Request.get_data",
        functools.partial(if_iast_taint_returned_object_for, "http.request.body"),
    )
    if flask_version < (2, 0, 0):
        _w(
            "werkzeug._internal",
            "_DictAccessorProperty.__get__",
            functools.partial(if_iast_taint_returned_object_for, "http.request.querystring"),
        )

    # flask.app.Flask methods that have custom tracing (add metadata, wrap functions, etc)
    _w("flask", "Flask.wsgi_app", traced_wsgi_app)
    _w("flask", "Flask.dispatch_request", request_tracer("dispatch_request"))
    _w("flask", "Flask.preprocess_request", request_tracer("preprocess_request"))
    _w("flask", "Flask.add_url_rule", traced_add_url_rule)
    _w("flask", "Flask.endpoint", traced_endpoint)
    if flask_version >= (2, 0, 0):
        _w("flask", "Flask.register_error_handler", traced_register_error_handler)
    else:
        _w("flask", "Flask._register_error_handler", traced__register_error_handler)

    # flask.blueprints.Blueprint methods that have custom tracing (add metadata, wrap functions, etc)
    _w("flask", "Blueprint.register", traced_blueprint_register)
    _w("flask", "Blueprint.add_url_rule", traced_blueprint_add_url_rule)

    # flask.app.Flask traced hook decorators
    flask_hooks = [
        "before_request",
        "before_first_request",
        "after_request",
        "teardown_request",
        "teardown_appcontext",
    ]
    for hook in flask_hooks:
        _w("flask", "Flask.{}".format(hook), traced_flask_hook)
    _w("flask", "after_this_request", traced_flask_hook)

    # flask.app.Flask traced methods
    flask_app_traces = [
        "process_response",
        "handle_exception",
        "handle_http_exception",
        "handle_user_exception",
        "do_teardown_request",
        "do_teardown_appcontext",
        "send_static_file",
    ]
    if flask_version < (2, 2, 0):
        flask_app_traces.append("try_trigger_before_first_request_functions")

    for name in flask_app_traces:
        _w("flask", "Flask.{}".format(name), simple_tracer("flask.{}".format(name)))

    # flask static file helpers
    _w("flask", "send_file", simple_tracer("flask.send_file"))

    # flask.json.jsonify
    _w("flask", "jsonify", traced_jsonify)

    # flask.templating traced functions
    _w("flask.templating", "_render", traced_render)
    _w("flask", "render_template", traced_render_template)
    _w("flask", "render_template_string", traced_render_template_string)

    # flask.blueprints.Blueprint traced hook decorators
    bp_hooks = [
        "after_app_request",
        "after_request",
        "before_app_first_request",
        "before_app_request",
        "before_request",
        "teardown_request",
        "teardown_app_request",
    ]
    for hook in bp_hooks:
        _w("flask", "Blueprint.{}".format(hook), traced_flask_hook)

    # flask.signals signals
    if config.flask["trace_signals"]:
        signals = [
            "template_rendered",
            "request_started",
            "request_finished",
            "request_tearing_down",
            "got_request_exception",
            "appcontext_tearing_down",
        ]
        # These were added in 0.11.0
        if flask_version >= (0, 11):
            signals.append("before_render_template")

        # These were added in 0.10.0
        if flask_version >= (0, 10):
            signals.append("appcontext_pushed")
            signals.append("appcontext_popped")
            signals.append("message_flashed")

        for signal in signals:
            module = "flask"

            # v0.9 missed importing `appcontext_tearing_down` in `flask/__init__.py`
            #  https://github.com/pallets/flask/blob/0.9/flask/__init__.py#L35-L37
            #  https://github.com/pallets/flask/blob/0.9/flask/signals.py#L52
            # DEV: Version 0.9 doesn't have a patch version
            if flask_version <= (0, 9) and signal == "appcontext_tearing_down":
                module = "flask.signals"

            # DEV: Patch `receivers_for` instead of `connect` to ensure we don't mess with `disconnect`
            _w(module, "{}.receivers_for".format(signal), traced_signal_receivers_for(signal))


def unpatch():
    if not getattr(flask, "_datadog_patch", False):
        return
    setattr(flask, "_datadog_patch", False)

    props = [
        # Flask
        "Flask.wsgi_app",
        "Flask.dispatch_request",
        "Flask.add_url_rule",
        "Flask.endpoint",
        "Flask.preprocess_request",
        "Flask.process_response",
        "Flask.handle_exception",
        "Flask.handle_http_exception",
        "Flask.handle_user_exception",
        "Flask.do_teardown_request",
        "Flask.do_teardown_appcontext",
        "Flask.send_static_file",
        # Flask Hooks
        "Flask.before_request",
        "Flask.before_first_request",
        "Flask.after_request",
        "Flask.teardown_request",
        "Flask.teardown_appcontext",
        # Blueprint
        "Blueprint.register",
        "Blueprint.add_url_rule",
        # Blueprint Hooks
        "Blueprint.after_app_request",
        "Blueprint.after_request",
        "Blueprint.before_app_first_request",
        "Blueprint.before_app_request",
        "Blueprint.before_request",
        "Blueprint.teardown_request",
        "Blueprint.teardown_app_request",
        # Signals
        "template_rendered.receivers_for",
        "request_started.receivers_for",
        "request_finished.receivers_for",
        "request_tearing_down.receivers_for",
        "got_request_exception.receivers_for",
        "appcontext_tearing_down.receivers_for",
        # Top level props
        "after_this_request",
        "send_file",
        "jsonify",
        "render_template",
        "render_template_string",
        "templating._render",
    ]

    if flask_version >= (2, 0, 0):
        props.append("Flask.register_error_handler")
    else:
        props.append("Flask._register_error_handler")

    # These were added in 0.11.0
    if flask_version >= (0, 11):
        props.append("before_render_template.receivers_for")

    # These were added in 0.10.0
    if flask_version >= (0, 10):
        props.append("appcontext_pushed.receivers_for")
        props.append("appcontext_popped.receivers_for")
        props.append("message_flashed.receivers_for")

    # These were removed in 2.2.0
    if flask_version < (2, 2, 0):
        props.append("Flask.try_trigger_before_first_request_functions")

    for prop in props:
        # Handle 'flask.request_started.receivers_for'
        obj = flask

        # v0.9.0 missed importing `appcontext_tearing_down` in `flask/__init__.py`
        #  https://github.com/pallets/flask/blob/0.9/flask/__init__.py#L35-L37
        #  https://github.com/pallets/flask/blob/0.9/flask/signals.py#L52
        # DEV: Version 0.9 doesn't have a patch version
        if flask_version <= (0, 9) and prop == "appcontext_tearing_down.receivers_for":
            obj = flask.signals

        if "." in prop:
            attr, _, prop = prop.partition(".")
            obj = getattr(obj, attr, object())
        _u(obj, prop)


@with_instance_pin
def traced_wsgi_app(pin, wrapped, instance, args, kwargs):
    """
    Wrapper for flask.app.Flask.wsgi_app

    This wrapper is the starting point for all requests.
    """
    # DEV: This is safe before this is the args for a WSGI handler
    #   https://www.python.org/dev/peps/pep-3333/
    environ, start_response = args
    middleware = _FlaskWSGIMiddleware(wrapped, pin.tracer, config.flask, pin)
    return middleware(environ, start_response)


def traced_blueprint_register(wrapped, instance, args, kwargs):
    """
    Wrapper for flask.blueprints.Blueprint.register

    This wrapper just ensures the blueprint has a pin, either set manually on
    itself from the user or inherited from the application
    """
    app = get_argument_value(args, kwargs, 0, "app")
    # Check if this Blueprint has a pin, otherwise clone the one from the app onto it
    pin = Pin.get_from(instance)
    if not pin:
        pin = Pin.get_from(app)
        if pin:
            pin.clone().onto(instance)
    return wrapped(*args, **kwargs)


def traced_blueprint_add_url_rule(wrapped, instance, args, kwargs):
    pin = Pin._find(wrapped, instance)
    if not pin:
        return wrapped(*args, **kwargs)

    def _wrap(rule, endpoint=None, view_func=None, **kwargs):
        if view_func:
            pin.clone().onto(view_func)
        return wrapped(rule, endpoint=endpoint, view_func=view_func, **kwargs)

    return _wrap(*args, **kwargs)


def traced_add_url_rule(wrapped, instance, args, kwargs):
    """Wrapper for flask.app.Flask.add_url_rule to wrap all views attached to this app"""

    def _wrap(rule, endpoint=None, view_func=None, **kwargs):
        if view_func:
            # TODO: `if hasattr(view_func, 'view_class')` then this was generated from a `flask.views.View`
            #   should we do something special with these views? Change the name/resource? Add tags?
            view_func = wrap_view(instance, view_func, name=endpoint, resource=rule)

        return wrapped(rule, endpoint=endpoint, view_func=view_func, **kwargs)

    return _wrap(*args, **kwargs)


def traced_endpoint(wrapped, instance, args, kwargs):
    """Wrapper for flask.app.Flask.endpoint to ensure all endpoints are wrapped"""
    endpoint = kwargs.get("endpoint", args[0])

    def _wrapper(func):
        # DEV: `wrap_function` will call `func_name(func)` for us
        return wrapped(endpoint)(wrap_function(instance, func, resource=endpoint))

    return _wrapper


def traced_flask_hook(wrapped, instance, args, kwargs):
    """Wrapper for hook functions (before_request, after_request, etc) are properly traced"""
    func = get_argument_value(args, kwargs, 0, "f")
    return wrapped(wrap_function(instance, func))


def traced_render_template(wrapped, instance, args, kwargs):
    """Wrapper for flask.templating.render_template"""
    pin = Pin._find(wrapped, instance, get_current_app())
    if not pin or not pin.enabled():
        return wrapped(*args, **kwargs)

    with pin.tracer.trace("flask.render_template", span_type=SpanTypes.TEMPLATE) as span:
        span.set_tag_str(COMPONENT, config.flask.integration_name)

        return wrapped(*args, **kwargs)


def traced_render_template_string(wrapped, instance, args, kwargs):
    """Wrapper for flask.templating.render_template_string"""
    pin = Pin._find(wrapped, instance, get_current_app())
    if not pin or not pin.enabled():
        return wrapped(*args, **kwargs)

    with pin.tracer.trace("flask.render_template_string", span_type=SpanTypes.TEMPLATE) as span:
        span.set_tag_str(COMPONENT, config.flask.integration_name)

        return wrapped(*args, **kwargs)


def traced_render(wrapped, instance, args, kwargs):
    """
    Wrapper for flask.templating._render

    This wrapper is used for setting template tags on the span.

    This method is called for render_template or render_template_string
    """
    pin = Pin._find(wrapped, instance, get_current_app())
    span = pin.tracer.current_span()

    if not pin.enabled or not span:
        return wrapped(*args, **kwargs)

    def _wrap(template, context, app):
        name = maybe_stringify(getattr(template, "name", None) or config.flask.get("template_default_name"))
        if name is not None:
            span.resource = name
            span.set_tag_str("flask.template_name", name)
        return wrapped(*args, **kwargs)

    return _wrap(*args, **kwargs)


def traced__register_error_handler(wrapped, instance, args, kwargs):
    """Wrapper to trace all functions registered with flask.app._register_error_handler"""

    def _wrap(key, code_or_exception, f):
        return wrapped(key, code_or_exception, wrap_function(instance, f))

    return _wrap(*args, **kwargs)


def traced_register_error_handler(wrapped, instance, args, kwargs):
    """Wrapper to trace all functions registered with flask.app.register_error_handler"""

    def _wrap(code_or_exception, f):
        return wrapped(code_or_exception, wrap_function(instance, f))

    return _wrap(*args, **kwargs)


def _set_block_tags(span):
    span.set_tag_str(http.STATUS_CODE, "403")
    request = flask.request
    try:
        base_url = getattr(request, "base_url", None)
        query_string = getattr(request, "query_string", None)
        if base_url and query_string:
            _set_url_tag(config.flask, span, base_url, query_string)
        if query_string and config.flask.trace_query_string:
            span.set_tag_str(http.QUERY_STRING, query_string)
        if request.method is not None:
            span.set_tag_str(http.METHOD, request.method)
        user_agent = _get_request_header_user_agent(request.headers)
        if user_agent:
            span.set_tag_str(http.USER_AGENT, user_agent)
    except Exception as e:
        log.warning("Could not set some span tags on blocked request: %s", str(e))  # noqa: G200


def _block_request_callable(span):
    request = flask.request
    _context.set_item("http.request.blocked", True, span=span)
    _set_block_tags(span)
    ctype = "text/html" if "text/html" in request.headers.get("Accept", "").lower() else "text/json"
    abort(flask.Response(utils._get_blocked_template(ctype), content_type=ctype, status=403))


def request_tracer(name):
    @with_instance_pin
    def _traced_request(pin, wrapped, instance, args, kwargs):
        """
        Wrapper to trace a Flask function while trying to extract endpoint information
          (endpoint, url_rule, view_args, etc)

        This wrapper will add identifier tags to the current span from `flask.app.Flask.wsgi_app`.
        """
        span = pin.tracer.current_span()
        if not pin.enabled or not span:
            return wrapped(*args, **kwargs)

        # This call may be unnecessary since we try to add the tags earlier
        # We just haven't been able to confirm this yet
        _set_request_tags(span)

        with pin.tracer.trace(
            ".".join(("flask", name)),
            service=trace_utils.int_service(pin, config.flask, pin),
        ) as request_span:
            _asm_request_context.set_block_request_callable(functools.partial(_block_request_callable, span))
            request_span.set_tag_str(COMPONENT, config.flask.integration_name)

            request_span._ignore_exception(werkzeug.exceptions.NotFound)
            if config._appsec_enabled and _context.get_item("http.request.blocked", span=span):
                _asm_request_context.block_request()
            return wrapped(*args, **kwargs)

    return _traced_request


def traced_signal_receivers_for(signal):
    """Wrapper for flask.signals.{signal}.receivers_for to ensure all signal receivers are traced"""

    def outer(wrapped, instance, args, kwargs):
        sender = get_argument_value(args, kwargs, 0, "sender")
        # See if they gave us the flask.app.Flask as the sender
        app = None
        if isinstance(sender, flask.Flask):
            app = sender
        for receiver in wrapped(*args, **kwargs):
            yield wrap_signal(app, signal, receiver)

    return outer


def traced_jsonify(wrapped, instance, args, kwargs):
    pin = Pin._find(wrapped, instance, get_current_app())
    if not pin or not pin.enabled():
        return wrapped(*args, **kwargs)

    with pin.tracer.trace("flask.jsonify") as span:
        span.set_tag_str(COMPONENT, config.flask.integration_name)

        return wrapped(*args, **kwargs)


def _set_request_tags(span):
    try:
        # raises RuntimeError if a request is not active:
        # https://github.com/pallets/flask/blob/2.1.3/src/flask/globals.py#L40
        request = flask.request

        span.set_tag_str(COMPONENT, config.flask.integration_name)

        if span.name.split(".")[-1] == "request":
            span.set_tag_str(SPAN_KIND, SpanKind.SERVER)

        # DEV: This name will include the blueprint name as well (e.g. `bp.index`)
        if not span.get_tag(FLASK_ENDPOINT) and request.endpoint:
            span.resource = " ".join((request.method, request.endpoint))
            span.set_tag_str(FLASK_ENDPOINT, request.endpoint)

        if not span.get_tag(FLASK_URL_RULE) and request.url_rule and request.url_rule.rule:
            span.resource = " ".join((request.method, request.url_rule.rule))
            span.set_tag_str(FLASK_URL_RULE, request.url_rule.rule)

        if not span.get_tag(FLASK_VIEW_ARGS) and request.view_args and config.flask.get("collect_view_args"):
            for k, v in request.view_args.items():
                # DEV: Do not use `set_tag_str` here since view args can be string/int/float/path/uuid/etc
                #      https://flask.palletsprojects.com/en/1.1.x/api/#url-route-registrations
                span.set_tag(".".join((FLASK_VIEW_ARGS, k)), v)
            trace_utils.set_http_meta(span, config.flask, request_path_params=request.view_args)
    except Exception:
        log.debug('failed to set tags for "flask.request" span', exc_info=True)
