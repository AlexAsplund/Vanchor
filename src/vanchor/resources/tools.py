from pyproj import Geod
import numpy as np
from math import radians, cos, sin, asin, sqrt, pi, atan2, degrees, acos


class Tools:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.converter = Converter(main)
        self.geo = Geo(main, self.converter)

    def angle_of_vector(self, x, y):
        return degrees(atan2(-y, x))

    def angle_of_line(self, x1, y1, x2, y2):
        return degrees(atan2(-y1 - y2, x2 - x1))

    def get_angle(self, a, b):
        vec1 = 100 * cos(a * pi / 180), 100 * -sin(a * pi / 180)
        vec2 = 100 * cos(b * pi / 180), 100 * -sin(b * pi / 180)
        v1 = self.angle_of_vector(*vec1)
        v2 = self.angle_of_vector(*vec2)

        cw = abs(v1 - v2) % 360
        ccw = 360 - abs(v1 - v2) % 360

        if cw < ccw:
            lowest = cw
        else:
            lowest = ccw
        return [cw, ccw, lowest]

    def get_cross_track_error(self, start, end, current):
        return self.geo.getCrossTrackDistance(start, end, current)


class Converter:

    geodesic = Geod(ellps="WGS84")

    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main

    def from_dms_to_dd(self, lat, lat_dir, lon, lon_dir):

        # Calculate lat
        lat_degrees = int(lat[0:2])
        lat_minutes = float((lat[2:]))

        if lat_dir == "S":
            lat_dd = (lat_degrees + (lat_minutes / 60)) * -1
        else:
            lat_dd = lat_degrees + (lat_minutes / 60)

        # Calculate lon
        lon_degrees = int(lon[0:2])
        lon_minutes = float((lon[2:]))

        if lat_dir == "W":
            lon_dd = (lon_degrees + (lat_minutes / 60)) * -1
        else:
            lon_dd = lon_degrees + (lat_minutes / 60)

        return [lat_dd, lon_dd]

    def get_bearing(self, a, b):

        self.logger.debug("A: {},{} B: {},{}".format(a[0], a[1], b[0], b[1]))
        fwd_azimuth, back_azimuth, distance = self.geodesic.inv(a[0], a[1], b[0], b[1])

        return {
            "ForwardAzimuth": round(fwd_azimuth, 3),
            "BackAzimuth": round(back_azimuth, 3),
            "Distance": round(distance, 3),
        }


class Geo:
    def __init__(self, main, converter):
        self.geodesic = converter.geodesic
        self.converter = converter
        self.main = main
        self.logger = main.logging.getLogger(self.__class__.__name__)

    def get_coordinates_from_heading(self, coordinates, distance, heading):
        # https://stackoverflow.com/questions/7222382/get-lat-long-given-current-point-distance-and-bearing
        R = self.geodesic.a
        brng = radians(heading)
        d = distance

        lat1 = radians(coordinates[0])
        lon1 = radians(coordinates[1])

        lat2 = asin(sin(lat1) * cos(d / R) + cos(lat1) * sin(d / R) * cos(brng))

        lon2 = lon1 + atan2(
            sin(brng) * sin(d / R) * cos(lat1), cos(d / R) - sin(lat1) * sin(lat2)
        )

        lat2 = degrees(lat2)
        lon2 = degrees(lon2)

        return [lat2, lon2]

    # Below is taken from: https://gis.stackexchange.com/questions/209540/projecting-cross-track-distance-on-great-circle
    def spherical2Cart(self, lat, lon):
        clat = (90 - lat) * np.pi / 180.0
        lon = lon * np.pi / 180.0
        x = np.cos(lon) * np.sin(clat)
        y = np.sin(lon) * np.sin(clat)
        z = np.cos(clat)

        return np.array([x, y, z])

    def cart2Spherical(self, x, y, z):
        r = np.sqrt(x ** 2 + y ** 2 + z ** 2)
        clat = np.arccos(z / r) / np.pi * 180
        lat = 90.0 - clat
        lon = np.arctan2(y, x) / np.pi * 180
        lon = (lon + 360) % 360

        return np.array([lat, lon, np.ones(lat.shape)])

    def greatCircle(self, lat1, lon1, lat2, lon2, r=None, verbose=False):
        """Compute the great circle distance on a sphere

        <lat1>, <lat2>: scalar float or nd-array, latitudes in degree for
                        location 1 and 2.
        <lon1>, <lon2>: scalar float or nd-array, longitudes in degree for
                        location 1 and 2.

        <r>: scalar float, spherical radius.

        Return <arc>: great circle distance on sphere.
        """

        if r is None:
            r = self.geodesic.a / 1000  # km

        d2r = lambda x: x * np.pi / 180
        lat1, lon1, lat2, lon2 = map(d2r, [lat1, lon1, lat2, lon2])
        dlon = abs(lon1 - lon2)

        numerator = (cos(lat2) * sin(dlon)) ** 2 + (
            cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
        ) ** 2
        numerator = np.sqrt(numerator)
        denominator = sin(lat1) * sin(lat2) + cos(lat1) * cos(lat2) * cos(dlon)

        dsigma = np.arctan2(numerator, denominator)
        arc = r * dsigma

        return arc

    def getCrossTrackPoint(self, start, end, current):
        """Get the closest point on great circle path to the 3rd point

        <lat1>, <lon1>: scalar float or nd-array, latitudes and longitudes in
                        degree, start point of the great circle.
        <lat2>, <lon2>: scalar float or nd-array, latitudes and longitudes in
                        degree, end point of the great circle.
        <lat3>, <lon3>: scalar float or nd-array, latitudes and longitudes in
                        degree, a point away from the great circle.

        Return <latp>, <lonp>: latitude and longitude of point P on the great
                            circle that connects P1, P2, and is closest
                            to point P3.
        """
        lat1, lon1, lat2, lon2, lat3, lon3 = start + end + current
        x1, y1, z1 = self.spherical2Cart(lat1, lon1)
        x2, y2, z2 = self.spherical2Cart(lat2, lon2)
        x3, y3, z3 = self.spherical2Cart(lat3, lon3)

        D, E, F = np.cross([x1, y1, z1], [x2, y2, z2])

        a = E * z3 - F * y3
        b = F * x3 - D * z3
        c = D * y3 - E * x3

        f = c * E - b * F
        g = a * F - c * D
        h = b * D - a * E

        tt = np.sqrt(f ** 2 + g ** 2 + h ** 2)
        xp = f / tt
        yp = g / tt
        zp = h / tt

        result1 = self.cart2Spherical(xp, yp, zp)
        result2 = self.cart2Spherical(-xp, -yp, -zp)
        d1 = self.greatCircle(result1[0], result1[1], lat3, lon3, r=1.0)
        d2 = self.greatCircle(result2[0], result2[1], lat3, lon3, r=1.0)

        if d1 > d2:
            return [result2[0], result2[1]]
        else:
            return [result1[0], result1[1]]

    def getCrossTrackDistance(self, start, end, current, r=None):
        """Compute cross-track distance
        <lat1>, <lon1>: scalar float or nd-array, latitudes and longitudes in
                        degree, start point of the great circle.
        <lat2>, <lon2>: scalar float or nd-array, latitudes and longitudes in
                        degree, end point of the great circle.
        <lat3>, <lon3>: scalar float or nd-array, latitudes and longitudes in
                        degree, a point away from the great circle.
        Return <dxt>: great cicle distance between point P3 to the closest point
                    on great circle that connects P1 and P2.
                    NOTE that the sign of dxt tells which side of the 3rd point
                    P3 is on.
        """

        cross_track_point = self.getCrossTrackPoint(start, end, current)
        dxt = self.converter.get_bearing(current, cross_track_point)["Distance"]
        return dxt

    def getBearing(self, start, end):
        import math

        lat1, long1 = start
        lat2, long2 = end
        dLon = long2 - long1
        y = math.sin(dLon) * math.cos(lat2)
        x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(
            lat2
        ) * math.cos(dLon)
        brng = math.atan2(y, x)
        brng = np.rad2deg(brng)
        return brng
