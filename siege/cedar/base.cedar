// Status quo: everything the agent can do is allowed (plan §6.3, D4).
// Fixes are forbid policies layered on top; in Cedar a forbid overrides a permit,
// so each fix removes exactly one class of abuse and leaves everything else working.
permit (principal, action, resource);
