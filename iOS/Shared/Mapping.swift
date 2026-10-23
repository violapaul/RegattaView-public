//
//  Mapping.swift
//  GPSTest
//
//  Created by Paul Viola on 10/29/22.
//

import SwiftUI
import Foundation
import CoreLocation
import simd
import Spatial

/// Mapping contains types and functions that support mapping, particularly moving from LLA to NED, etc.
class Mapping {
    /// All the same, but can help in documenting some of what we do.
    public typealias Coord = Vector // used for lat/lon/alt
    public typealias Point = Vector  // used for 2D NED coords, homogenous
    
    /// Store the starting line as a pair of points on the line.
    public struct Line {
        var from: Point
        var to: Point
        var vPerp: Vector  /// Unit vector which is perpendicular to the line
        var vAlong: Vector /// Unit vector which points along the line.
    }
    
    
    init() {
        // do nothing
    }
    
    static func location2LLA(_ location: CLLocation) -> Coord {
        let c = location.coordinate
        return Vector(c.latitude, c.longitude,  0.0)
    }
    
    struct Ellipsoid {
        var semimajorAxis = 6378137.0
        var semiminorAxis = 6356752.31424518
    }
    
    public static func deg2rad(_ number: Double) -> Double {
        return 2.0 * .pi * (number / 360.0)
    }

    public static func rad2deg(_ number: Double) -> Double {
        return 360.0 * number / (2 * .pi)
    }
    
    // Points and lines are all 2D, though we will use hogenous coordinates.
    
    /// Convert a coordinate into a homogenous 2D point
    public static func toPoint (_ from: Coord) -> Point {
        return toPoint(from.x, from.y)
    }
    
    /// Convert a 2D location to a point
    public static func toPoint (_ north: Double, _ east: Double) -> Point {
        return Vector(north, east, 1.0)
    }
    
    /// Given two NED coordinates, create a start line.
    /// First bit is a perpendicular vector that returns positive values for points to the left of the line.
    /// Second bit is a parallel vector that returns 1.0 for the TO point.
    public static func makeLine(from: Coord, to:Coord) -> Line {
        var vAlong = to - from
        // Perpendicular to the line
        var vPerp = Vector(vAlong.y, -vAlong.x, 0.0) // If the boat is FROM and pin is TO, then points to the left are positive distances.

        vPerp = vPerp/vPerp.length  // Normalize
        vAlong = vAlong/vAlong.length  // Normalize
        
        return Line(from: from, to: to, vPerp: vPerp, vAlong: vAlong)
    }
    
    /// Perpendicular projection of the point on the line.
    public static func projectPoint(line: Line, point: Point) -> Point {
        let fromToPoint = point - line.from  // From centered
        let parallelLength = fromToPoint.dot(line.vAlong) // distance on "along" axis
        return parallelLength * line.vAlong + line.from
    }
    
    public static func distance2Line(line: Line, point: Point) -> Double {
        let boatToPoint = point - line.from
        return line.vPerp.dot(boatToPoint)
    }
    
    public static func distanceAlongLine(line: Line, point: Point) -> Double {
        let boatToPoint = point - line.from
        return line.vAlong.dot(boatToPoint)
    }

    /// Return the position along the line normalized so that from is 0.0 and to is 1.0.
    public static func distanceAlongLineNormalized(line: Line, point: Point) -> Double {
        return distanceAlongLine(line: line, point: point) / (line.to - line.from).length
    }
    
    /// The compass angle of the heading FROM -> TO
    public static func compassAngleOfLine(line: Line) -> Double {
        let angle = Angle.radians(atan2(line.vAlong.y, line.vAlong.x)).degrees  // Convert to degrees
        return Mapping.compassAngle(angle)
    }
    
    static func geodetic2ecef(lat: Double,
                       lon: Double,
                       alt: Double,
                       ell: Ellipsoid? = nil,
                       deg: Bool = true) -> Vector {
        
        // sanitize the input
        let (sanitizedLat, sanitizedEll) = sanitize(lat: lat, ell: ell, deg: deg)
        
        // convert longitude to radians if necessary
        var convertedLon = lon
        if deg {
            convertedLon = convertedLon * Double.pi / 180
        }
        
        // compute the radius of curvature of the prime vertical section
        let N = pow(sanitizedEll.semimajorAxis, 2) / sqrt(pow(sanitizedEll.semimajorAxis, 2) * pow(cos(sanitizedLat), 2) + pow(sanitizedEll.semiminorAxis, 2) * pow(sin(sanitizedLat), 2))
        
        // compute the cartesian (geocentric) coordinates
        let x = (N + alt) * cos(sanitizedLat) * cos(convertedLon)
        let y = (N + alt) * cos(sanitizedLat) * sin(convertedLon)
        let z = (N * pow((sanitizedEll.semiminorAxis / sanitizedEll.semimajorAxis), 2) + alt) * sin(sanitizedLat)
        
        return Vector(x, y, z)
    }


    static func sanitize(lat: Double, ell: Ellipsoid?, deg: Bool) -> (lat: Double, ell: Ellipsoid) {
        
        // use a default ellipsoid if one is not provided
        let sanitizedEll: Ellipsoid
        if let ell = ell {
            sanitizedEll = ell
        } else {
            sanitizedEll = Ellipsoid()
        }
        
        // convert latitude to radians if necessary
        var sanitizedLat = lat
        if deg {
            sanitizedLat = sanitizedLat * Double.pi / 180
        }
        
        // ensure latitude is within acceptable bounds
        if abs(sanitizedLat) > Double.pi / 2 {
            fatalError("-pi/2 <= latitude <= pi/2")
        }
        
        return (sanitizedLat, sanitizedEll)
    }

    static func uvw2ned(u: Double, v: Double, w: Double, lat0: Double, lon0: Double, deg: Bool = true) -> Vector {
        
        // convert latitude and longitude to radians if necessary
        var convertedLat0 = lat0
        var convertedLon0 = lon0
        if deg {
            convertedLat0 = lat0 * Double.pi / 180
            convertedLon0 = lon0 * Double.pi / 180
        }
        
        // compute the ENU coordinates
        let t = cos(convertedLon0) * u + sin(convertedLon0) * v
        let east = -sin(convertedLon0) * u + cos(convertedLon0) * v
        let up = cos(convertedLat0) * t + sin(convertedLat0) * w
        let north = -sin(convertedLat0) * t + cos(convertedLat0) * w
        
        return Vector(north, east, -up)
    }

    static func geodetic2ned(
        lat: Double,
        lon: Double,
        h: Double,
        lat0: Double,
        lon0: Double,
        h0: Double,
        ell: Ellipsoid? = nil,
        deg: Bool = true) -> Vector {
            
            // convert geodetic coordinates to ECEF
            let v1 = geodetic2ecef(lat: lat, lon: lon, alt: h, ell: ell, deg: deg)
            let v2 = geodetic2ecef(lat: lat0, lon: lon0, alt: h0, ell: ell, deg: deg)
            let d = v1 - v2
            
            // use uvw2enu to compute the ENU coordinates
            return uvw2ned(u: d.x, v: d.y, w: d.z, lat0: lat0, lon0: lon0, deg: deg)
        }
    
    static func LLA2NED(lla: Vector, lla0: Vector) -> Vector {
        return geodetic2ned(lat: lla.lat, lon: lla.lon, h: 0.0, lat0: lla0.lat, lon0: lla0.lon, h0: 0.0)
    }
    
    static func test() {
        let res = Vector(-2300858.613016999, -3638994.9216960655, 4690005.776550657)
        let ggg = geodetic2ecef(lat: 47.6400825, lon: -122.3042916, alt: 0.0)
        print(res - ggg)
        
        let boat = Vector(47.640067, -122.3043352, 0.0)
        let boatE = Vector(-2300862.062696928, -3638994.247149957, 4690004.61538782)
        print(geodetic2ecef(lat: boat.lat, lon: boat.lon, alt: 0.0) - boatE)
        
        let pt = Vector(47.6400957, -122.3043381, 0.0)
        let ptE = Vector(-2300860.986789635, -3638992.137756393, 4690006.765411642)
        
        print(geodetic2ecef(lat: pt.lat, lon: pt.lon, alt: 0.0) - ptE)
        
        print("Ned test")
        let ned = Vector(3.1909612102950815, -0.21791444231483414, 8.031146072617901e-07)
        print(geodetic2ned(lat: pt.lat, lon: pt.lon, h: 0.0, lat0: boat.lat, lon0: boat.lon, h0: 0) - ned)
    }
    
    /// Given an arbitrary angle, wrap so that it is a compass angle.
    static func compassAngle(_ degrees: Double) -> Double {
        let r = degrees.truncatingRemainder(dividingBy: 360.0)
        if r < 0.0 {
            return r + 360.0
        }
        return r
    }
    
    // take a compass angle and convert to signed or relative angle.
    static func signedAngle(_ degrees: Double) -> Double {
        let r = degrees.truncatingRemainder(dividingBy: 360.0)
        if r > 180.0 {
            return r - 360.0
        }
        if r < -180.0 {
            return r + 360.0
        }
        return r
    }
}

