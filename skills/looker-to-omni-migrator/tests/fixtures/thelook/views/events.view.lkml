view: events {
  sql_table_name: thelook_ecommerce.events ;;

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: user_id {
    type: number
    sql: ${TABLE}.user_id ;;
  }

  dimension: event_type {
    type: string
    sql: ${TABLE}.event_type ;;
  }

  dimension: browser {
    type: string
    sql: ${TABLE}.browser ;;
  }

  dimension_group: created {
    type: time
    timeframes: [raw, date, week]
    sql: ${TABLE}.created_at ;;
  }

  measure: count {
    type: count
  }
}
