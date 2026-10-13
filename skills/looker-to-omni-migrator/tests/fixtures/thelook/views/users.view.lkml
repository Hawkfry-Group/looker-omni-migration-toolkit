# Shoppers registered on the thelook sample store. One row per user.
view: users {
  sql_table_name: thelook_ecommerce.users ;;

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: first_name {
    type: string
    sql: ${TABLE}.first_name ;;
  }

  dimension: last_name {
    type: string
    sql: ${TABLE}.last_name ;;
  }

  dimension: full_name {
    type: string
    sql: CONCAT(${first_name}, ' ', ${last_name}) ;;
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

  dimension: gender {
    type: string
    sql: ${TABLE}.gender ;;
  }

  dimension: state {
    type: string
    sql: ${TABLE}.state ;;
  }

  dimension: country {
    type: string
    sql: ${TABLE}.country ;;
  }

  dimension: traffic_source {
    type: string
    sql: ${TABLE}.traffic_source ;;
  }

  dimension_group: created {
    type: time
    timeframes: [raw, date, month, year]
    sql: ${TABLE}.created_at ;;
  }

  parameter: date_granularity {
    type: unquoted
    allowed_value: { value: "month" }
    allowed_value: { value: "year" }
  }

  measure: count {
    type: count
    label: "Shopper Count"
  }

  measure: average_age {
    type: average
    sql: ${age} ;;
    value_format_name: decimal_2
  }
}
