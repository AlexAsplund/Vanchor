"""Navigation: from NMEA in to routes and derived geometry.

The navigator parses inbound NMEA (RMC/GGA/HDM/HDT/APB/DPT…) into the shared
``NavigationState`` — the single parse point for both simulated and real
sensors. The rest of the package builds on that: routes + waypoints,
water-following "take me here" routing, along-contour track generation, depth
maps + soundings, track/trip logging, the sensor guard, and the NMEA-over-TCP
bridge for feeding an external GPS/plotter.
"""
