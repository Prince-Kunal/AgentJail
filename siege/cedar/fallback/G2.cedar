// G2: internal notes are staff-only. No customer may read any notes,
// not even the notes about themselves.
forbid (
  principal,
  action == Action::"readNotes",
  resource
) unless {
  principal.role == "staff"
};
