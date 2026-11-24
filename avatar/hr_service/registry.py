from avatar_contract import manifest_digest

from . import lifecycle, recruitment

MODULES = {name: module for module in (recruitment, lifecycle) for name in module.ACTIONS}
ACTIONS = {name: module.ACTIONS[name] for name, module in MODULES.items()}
LEGACY_ACTIONS = recruitment.LEGACY_ACTIONS
PREVIOUS = recruitment.manifest()
PREVIOUS_FINGERPRINT = (PREVIOUS["contractVersion"], manifest_digest(PREVIOUS))
MANIFEST = {**PREVIOUS, "contractVersion": "1.1.0", "description": "HR 请假、招聘、Offer 和入职服务", "actions": ACTIONS}
FINGERPRINT = (MANIFEST["contractVersion"], manifest_digest(MANIFEST))


def release_configuration():
    """Trusted deployment pins, exported independently of the running service."""
    return {
        "contractVersion": FINGERPRINT[0], "manifestSha256": FINGERPRINT[1],
        "allowedActions": sorted(ACTIONS), "leaveActions": MANIFEST["leaveActions"],
        "compatibleActions": [{
            "contractVersion": PREVIOUS_FINGERPRINT[0], "manifestSha256": PREVIOUS_FINGERPRINT[1],
            "action": recruitment.ADVANCE, "actionSha256": manifest_digest(PREVIOUS["actions"][recruitment.ADVANCE]),
        }] if allows_previous(recruitment.ADVANCE, PREVIOUS_FINGERPRINT) else [],
    }


def allows_previous(action, fingerprint):
    return action == recruitment.ADVANCE and fingerprint == PREVIOUS_FINGERPRINT and ACTIONS[action] == PREVIOUS["actions"][action]


def execute(store, actor, name, params, operation):
    return MODULES[name].execute(store, actor, name, params, operation)


def operation_result(store, actor, name, operation):
    module = MODULES[name]
    if module is recruitment:
        return recruitment.operation_result(store, actor, operation)
    return module.operation_result(store, actor, name, operation)
