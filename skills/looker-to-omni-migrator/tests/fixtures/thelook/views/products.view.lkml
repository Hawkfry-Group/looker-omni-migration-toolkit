view: products {
  sql_table_name: thelook_ecommerce.products ;;

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: name {
    type: string
    sql: ${TABLE}.name ;;
  }

  dimension: brand {
    type: string
    sql: ${TABLE}.brand ;;
  }

  dimension: category {
    type: string
    sql: ${TABLE}.category ;;
  }

  dimension: department {
    type: string
    sql: ${TABLE}.department ;;
  }

  dimension: sku {
    type: string
    label: "SKU Code"
    sql: ${TABLE}.sku ;;
  }

  dimension: retail_price {
    type: number
    sql: ${TABLE}.retail_price ;;
    value_format_name: usd
  }

  dimension: distribution_center_id {
    type: number
    sql: ${TABLE}.distribution_center_id ;;
  }

  measure: count {
    type: count
  }

  measure: average_retail_price {
    type: average
    sql: ${retail_price} ;;
    value_format_name: usd
  }
}
