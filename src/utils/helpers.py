def format_currency(value):
    """Formats a numerical value into currency format."""
    return "${:,.2f}".format(value)

def calculate_yoy_growth(current_revenue, previous_revenue):
    """Calculates the year-over-year growth based on revenue data."""
    if previous_revenue == 0:
        return float('inf')  # Avoid division by zero
    return (current_revenue - previous_revenue) / previous_revenue * 100