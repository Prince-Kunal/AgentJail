// G4: a refund may never exceed the order's total -- not even on your own order.
// Independent of ownership (G1); both forbids can be enforced together.
forbid (
  principal,
  action == Action::"issueRefund",
  resource
) when {
  context.amount > resource.total
};
