view: orders {
  sql_table_name: thelook_ecommerce.orders ;;

  dimension: order_id {
    primary_key: yes
    type: number
    sql: ${TABLE}.order_id ;;
  }

  dimension: user_id {
    type: number
    sql: ${TABLE}.user_id ;;
  }

  dimension: status {
    type: string
    sql: ${TABLE}.status ;;
  }

  dimension: num_of_item {
    type: number
    sql: ${TABLE}.num_of_item ;;
  }

  dimension_group: created {
    type: time
    timeframes: [raw, date, month]
    sql: ${TABLE}.created_at ;;
  }

  measure: count {
    type: count
  }
}
