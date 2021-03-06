from __future__ import absolute_import, division, print_function

from six.moves.queue import LifoQueue

from pyro import poutine
from pyro.infer.util import is_validation_enabled
from pyro.poutine import Trace
from pyro.poutine.enumerate_messenger import EXPAND_DEFAULT
from pyro.poutine.util import prune_subsample_sites
from pyro.util import check_model_guide_match, check_site_shape


def iter_discrete_escape(trace, msg):
    return ((msg["type"] == "sample") and
            (not msg["is_observed"]) and
            (msg["infer"].get("enumerate") == "sequential") and  # only sequential
            (msg["name"] not in trace))


def iter_discrete_extend(trace, site, **ignored):
    values = site["fn"].enumerate_support(expand=site["infer"].get("expand", EXPAND_DEFAULT))
    for i, value in enumerate(values):
        extended_site = site.copy()
        extended_site["infer"] = site["infer"].copy()
        extended_site["infer"]["_enum_total"] = len(values)
        extended_site["value"] = value
        extended_trace = trace.copy()
        extended_trace.add_node(site["name"], **extended_site)
        yield extended_trace


def get_importance_trace(graph_type, max_iarange_nesting, model, guide, *args, **kwargs):
    """
    Returns a single trace from the guide, and the model that is run
    against it.
    """
    guide = poutine.broadcast(guide)
    model = poutine.broadcast(model)
    guide_trace = poutine.trace(guide, graph_type=graph_type).get_trace(*args, **kwargs)
    model_trace = poutine.trace(poutine.replay(model, trace=guide_trace),
                                graph_type=graph_type).get_trace(*args, **kwargs)
    if is_validation_enabled():
        check_model_guide_match(model_trace, guide_trace, max_iarange_nesting)

    guide_trace = prune_subsample_sites(guide_trace)
    model_trace = prune_subsample_sites(model_trace)

    model_trace.compute_log_prob()
    guide_trace.compute_score_parts()
    if is_validation_enabled():
        for site in model_trace.nodes.values():
            if site["type"] == "sample":
                check_site_shape(site, max_iarange_nesting)
        for site in guide_trace.nodes.values():
            if site["type"] == "sample":
                check_site_shape(site, max_iarange_nesting)

    return model_trace, guide_trace


def iter_discrete_traces(graph_type, fn, *args, **kwargs):
    """
    Iterate over all discrete choices of a stochastic function.

    When sampling continuous random variables, this behaves like `fn`.
    When sampling discrete random variables, this iterates over all choices.

    This yields traces scaled by the probability of the discrete choices made
    in the `trace`.

    :param str graph_type: The type of the graph, e.g. "flat" or "dense".
    :param callable fn: A stochastic function.
    :returns: An iterator over traces pairs.
    """
    queue = LifoQueue()
    queue.put(Trace())
    traced_fn = poutine.trace(
        poutine.queue(fn, queue, escape_fn=iter_discrete_escape, extend_fn=iter_discrete_extend),
        graph_type=graph_type)
    while not queue.empty():
        yield traced_fn.get_trace(*args, **kwargs)


def _config_enumerate(default, expand):

    def config_fn(site):
        if site["type"] != "sample" or site["is_observed"]:
            return {}
        if not getattr(site["fn"], "has_enumerate_support", False):
            return {}
        return {"enumerate": site["infer"].get("enumerate", default),
                "expand": site["infer"].get("expand", expand)}

    return config_fn


def config_enumerate(guide=None, default="sequential", expand=EXPAND_DEFAULT):
    """
    Configures each enumerable site a guide to enumerate with given method,
    ``site["infer"]["enumerate"] = default``. This can be used as either a
    function::

        guide = config_enumerate(guide)

    or as a decorator::

        @config_enumerate
        def guide1(*args, **kwargs):
            ...

        @config_enumerate(default="parallel", expand=False)
        def guide2(*args, **kwargs):
            ...

    This does not overwrite existing annotations ``infer={"enumerate": ...}``.

    :param callable guide: a pyro model that will be used as a guide in
        :class:`~pyro.infer.svi.SVI`.
    :param str default: Which enumerate strategy to use, one of
        "sequential", "parallel", or None.
    :param bool expand: Whether to expand enumerated sample values. See
        :meth:`~pyro.distributions.Distribution.enumerate_support` for details.
    :return: an annotated guide
    :rtype: callable
    """
    if default not in ["sequential", "parallel", None]:
        raise ValueError("Invalid default value. Expected 'sequential', 'parallel', or None, but got {}".format(
            repr(default)))
    if expand not in [True, False]:
        raise ValueError("Invalid expand value. Expected True or False, but got {}".format(repr(expand)))
    # Support usage as a decorator:
    if guide is None:
        return lambda guide: config_enumerate(guide, default=default, expand=expand)

    return poutine.infer_config(guide, config_fn=_config_enumerate(default, expand))
