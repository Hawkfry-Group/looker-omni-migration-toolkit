# Customers of the thelook e-commerce sample store. One row per registered customer.
view: users {
  sql_table_name: thelook_ecommerce.users ;;

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: email {
    type: string
    sql: ${TABLE}.email ;;
    required_access_grants: [can_view_pii]
  }

  dimension: age {
    type: number
    sql: ${TABLE}.age ;;
  }

  dimension: country {
    type: string
    sql: ${TABLE}.country ;;
    map_layer_name: countries
  }

  dimension: traffic_source {
    type: string
    sql: ${TABLE}.traffic_source ;;
    description: "Channel the customer arrived from, for example Search or Email"
  }

  dimension_group: created {
    type: time
    timeframes: [raw, date, month, year]
    sql: ${TABLE}.created_at ;;
  }

  measure: count {
    type: count
    label: "Customer Count"
  }
}
