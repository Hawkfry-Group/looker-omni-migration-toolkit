# Example access grants. User attribute names and values are illustrative.
access_grant: can_view_pii {
  user_attribute: department
  allowed_values: ["data_team"]
}

access_grant: can_view_finance {
  user_attribute: department
  allowed_values: ["data_team", "finance"]
}
