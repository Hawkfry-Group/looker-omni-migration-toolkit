view: inventory_items {
  sql_table_name: thelook_ecommerce.inventory_items ;;

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: product_id {
    type: number
    sql: ${TABLE}.product_id ;;
  }

  dimension: cost {
    type: number
    sql: ${TABLE}.cost ;;
    value_format_name: usd
  }

  dimension_group: sold {
    type: time
    timeframes: [raw, date, month]
    sql: ${TABLE}.sold_at ;;
  }

  measure: count {
    type: count
  }

  measure: total_cost {
    type: sum
    sql: ${cost} ;;
    value_format_name: usd
  }
}
