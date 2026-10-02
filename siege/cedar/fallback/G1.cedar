// G1 (and G3): a refund may be issued only by the order's owner.
// One policy covers both the direct attack and the indirect-injection path.
forbid (
  principal,
  action == Action::"issueRefund",
  resource
) unless {
  resource.owner == principal
};
