"""Kubernetes warning events, reduced to what they mean. Event messages are free text from kubelets and controllers and
can carry probe response bodies, tokens and passwords, so a message is never stored: only allowlisted reasons are
kept, each as a fixed description plus, where useful, a detail picked from a fixed vocabulary (scheduling reasons,
eviction pressure) or an image's repository name."""
from __future__ import annotations

import re

import iac_common as c

IMAGE = re.compile(r'image "?([A-Za-z0-9][A-Za-z0-9._/:@-]{0,200})')
SCHEDULING = (("Insufficient cpu", re.compile(r"Insufficient cpu", re.I)), ("Insufficient memory", re.compile(r"Insufficient memory", re.I)),
              ("Insufficient ephemeral-storage", re.compile(r"Insufficient ephemeral-storage", re.I)),
              ("Insufficient pods", re.compile(r"Insufficient pods|Too many pods", re.I)), ("Insufficient GPUs", re.compile(r"Insufficient nvidia\.com/gpu", re.I)),
              ("node selector or affinity", re.compile(r"didn't match (Pod's )?node (affinity|selector)", re.I)),
              ("untolerated taint", re.compile(r"untolerated taint|had taint", re.I)), ("volume node affinity", re.compile(r"volume node affinity", re.I)),
              ("unbound volume claim", re.compile(r"unbound immediate PersistentVolumeClaims", re.I)))
PRESSURE = (("memory pressure", re.compile(r"memory", re.I)), ("disk pressure", re.compile(r"ephemeral-storage|disk|nodefs|imagefs", re.I)))
PROBE = re.compile(r"^(Readiness|Liveness|Startup) probe", re.I)


def _image(message: str) -> str:
    m = IMAGE.search(message)
    name = c.image_engine(m.group(1).split("?")[0]) if m else ""
    return name if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name) else ""


def _vocab(message: str, words: tuple) -> str:
    return ", ".join(label for label, rx in words if rx.search(message))


def describe(reason: str, message: str) -> str | None:
    """'<Label>: <fixed description>[ (detail)]', or None for a reason that is not kept."""
    msg = message or ""
    pull = re.search(r"pull(ing)? image|ImagePullBackOff|ErrImagePull", msg, re.I)
    if reason in ("ErrImagePull", "ImagePullBackOff") or (reason in ("Failed", "BackOff") and pull):
        img = _image(msg)
        return "ImagePullBackOff: the image could not be pulled" + (f" ({img})" if img else "")
    if reason == "CrashLoopBackOff" or (reason == "BackOff" and re.search(r"restarting failed container", msg, re.I)):
        return "CrashLoopBackOff: the container keeps crashing and restarting"
    if reason == "BackOff":
        return "BackOff: Kubernetes is backing off before retrying"
    if reason == "FailedScheduling":
        why = _vocab(msg, SCHEDULING)
        return "FailedScheduling: no node could take the pod" + (f" ({why})" if why else "")
    if reason in ("OOMKilling", "OOMKilled"):
        return "OOMKilled: the container ran out of memory and was killed"
    if reason == "Evicted":
        why = _vocab(msg, PRESSURE)
        return "Evicted: the pod was evicted from its node" + (f" ({why})" if why else "")
    if reason == "Unhealthy":
        m = PROBE.match(msg)
        return f"Unhealthy: {m.group(1).lower() if m else 'readiness/liveness'} probe failed"
    fixed = {"Failed": "Failed: a container failed to start", "FailedMount": "FailedMount: a volume could not be mounted",
             "FailedAttachVolume": "FailedAttachVolume: a volume could not be attached", "FailedCreate": "FailedCreate: the controller could not create a pod"}
    return fixed.get(reason)
