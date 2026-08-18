"""
Trim Setting Lookup Module
Returns stabilizer trim setting based on aircraft type and CG position
Uses linear interpolation between data points
"""

def get_trim_setting(icaocode, cg_percent):
    """
    Get stabilizer trim setting for takeoff based on CG position.
    Uses linear interpolation between defined data points.
    
    Args:
        icaocode: Aircraft ICAO code (e.g., 'A320', 'A20N')
        cg_percent: Center of Gravity as percentage MAC (e.g., 25.5)
    
    Returns:
        dict with 'cg' and 'trim' values or None if not found
        Example: {'cg': '32.5', 'trim': 'N/U 1.2'}
    """
    
    if not cg_percent:
        return None
    
    try:
        cg = float(cg_percent)
    except (ValueError, TypeError):
        return None
    
    def interpolate(cg_value, data_points):
        """
        Linear interpolation between data points.
        data_points: list of tuples [(cg, trim), ...]
        """
        # Sort by CG value
        data_points = sorted(data_points, key=lambda x: x[0])
        
        # If CG is below minimum, use minimum value
        if cg_value <= data_points[0][0]:
            return data_points[0][1]
        
        # If CG is above maximum, use maximum value
        if cg_value >= data_points[-1][0]:
            return data_points[-1][1]
        
        # Find surrounding points and interpolate
        for i in range(len(data_points) - 1):
            cg1, trim1 = data_points[i]
            cg2, trim2 = data_points[i + 1]
            
            if cg1 <= cg_value <= cg2:
                # Linear interpolation formula
                ratio = (cg_value - cg1) / (cg2 - cg1)
                trim_value = trim1 + ratio * (trim2 - trim1)
                return trim_value
        
        return None
    
    # Airbus A321 - Degrees format (negative = N/U, positive = N/D)
    if icaocode in ['A321', 'A21N']:
        data_points = [
            (10, -4.5),
            (11, -4.5),
            (12, -4.5),
            (13, -4.2),
            (14, -3.9),
            (15, -3.7),
            (16, -3.4),
            (17, -3.1),
            (18, -2.8),
            (19, -2.6),
            (20, -2.3),
            (21, -2.0),
            (22, -1.7),
            (23, -1.5),
            (24, -1.2),
            (25, -0.9),
            (26, -0.6),
            (27, -0.4),
            (28, -0.1),
            (29, 0.2),
            (30, 0.5),
            (31, 0.7),
            (32, 1.0),
            (33, 1.3),
            (34, 1.6),
            (35, 1.8),
            (36, 2.1),
            (37, 2.4),
            (38, 2.7),
            (39, 2.9),
            (40, 3.2),
            (41, 3.5),
            (42, 3.5),
            (43, 3.5),
            (44, 3.5),
            (45, 3.5),
            (46, 3.5),
        ]
        trim_value = interpolate(cg, data_points)
        
        # Format as N/U or N/D
        if trim_value < 0:
            trim_display = f'N/U {abs(trim_value):.1f}'
        else:
            trim_display = f'N/D {trim_value:.1f}'
        
        return {'cg': f'{cg:.1f}', 'trim': trim_display}
    
    # Airbus A320 - Degrees format (positive = UP, negative = DN)
    elif icaocode in ['A320', 'A20N']:
        data_points = [
            (15, -2.5),
            (16, -2.5),
            (17, -2.5),
            (18, -2.3),
            (19, -2.1),
            (20, -1.8),
            (21, -1.6),
            (22, -1.4),
            (23, -1.2),
            (24, -1.0),
            (25, -0.8),
            (26, -0.5),
            (27, -0.3),
            (28, -0.1),
            (29, 0.1),
            (30, 0.3),
            (31, 0.5),
            (32, 0.8),
            (33, 1.0),
            (34, 1.3),
            (35, 1.4),
            (36, 1.6),
            (37, 1.8),
            (38, 2.1),
            (39, 2.3),
            (40, 2.5),
            (41, 2.5),
            (42, 2.5),
            (43, 2.5),
        ]
        trim_value = interpolate(cg, data_points)
        
        # Format as N/U or N/D
        if trim_value < 0:
            trim_display = f'N/U {abs(trim_value):.1f}'
        else:
            trim_display = f'N/D {trim_value:.1f}'
        
        return {'cg': f'{cg:.1f}', 'trim': trim_display}
    
    # Airbus A319 - Degrees format (positive = UP, negative = DN)
    # Embraer E175 - Flap 2 only, degrees UP
    elif icaocode == 'E75L' or icaocode == 'E175':
        # Flap 2 trim table (% MAC → trim degrees UP)
        trim_table_flap2 = {
            7: 6.0,
            9: 6.0,
            11: 5.5,
            13: 5.0,
            15: 4.5,
            17: 4.0,
            19: 3.5,
            21: 3.5,
            23: 3.0,
            25: 2.5,
            27: 2.0
        }

        cg_keys = sorted(trim_table_flap2.keys())
        if cg <= cg_keys[0]:
            trim_value = trim_table_flap2[cg_keys[0]]
        elif cg >= cg_keys[-1]:
            trim_value = trim_table_flap2[cg_keys[-1]]
        else:
            # Linear interpolation between nearest points
            for i in range(len(cg_keys) - 1):
                low, high = cg_keys[i], cg_keys[i + 1]
                if low <= cg <= high:
                    low_val = trim_table_flap2[low]
                    high_val = trim_table_flap2[high]
                    trim_value = low_val + (cg - low) * (high_val - low_val) / (high - low)
                    break

        trim_display = f'{trim_value:.1f}'
        return {'cg': f'{cg:.1f}', 'trim': trim_display}

    # Airbus A319 - Degrees format (positive = UP, negative = DN)
    elif icaocode == 'A319':
        data_points = [
            (15, -3.5),
            (16, -3.5),
            (17, -3.5),
            (18, -3.5),
            (19, -3.2),
            (20, -2.9),
            (21, -2.7),
            (22, -2.4),
            (23, -2.1),
            (24, -1.8),
            (25, -1.5),
            (26, -1.2),
            (27, -1.0),
            (28, -0.7),
            (29, -0.4),
            (30, -0.1),
            (31, 0.2),
            (32, 0.5),
            (33, 0.7),
            (34, 1.0),
            (35, 1.3),
            (36, 1.6),
            (37, 1.9),
            (38, 2.2),
            (39, 2.4),
            (40, 2.7),
        ]
        trim_value = interpolate(cg, data_points)
        if trim_value < 0:
            trim_display = f'N/U {abs(trim_value):.1f}'
        else:
            trim_display = f'N/D {trim_value:.1f}'
        return {'cg': f'{cg:.1f}', 'trim': trim_display}

    # Default/Unknown aircraft
    # Default/Unknown aircraft
    else:
        return None



# Test function
if __name__ == "__main__":
    # Test cases with interpolation
    test_cases = [
        ('A320', 32.5),   # Should interpolate between 32.0 and 33.0
        ('A320', 38.5),   # Should interpolate between 38.0 and 39.0
        ('A319', 25.0),   # Should interpolate
        ('A321', 28.3),   # Should interpolate
        ('A20N', 35.5),   # Should interpolate
        ('A21N', 40.0),   # Should be UP 3.2
    ]
    
    print("Trim Setting Lookup Tests (with interpolation):\n")
    for aircraft, cg in test_cases:
        result = get_trim_setting(aircraft, cg)
        if result:
            print(f"{aircraft} @ {cg}% MAC: Trim = {result['trim']}")
        else:
            print(f"{aircraft} @ {cg}% MAC: No data available")


            
