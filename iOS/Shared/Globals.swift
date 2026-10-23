//
//  Globals.swift
//  BoatStart
//
//  Created by Paul Viola on 4/8/23.
//

import Foundation

struct Globals {
    // This is the distance that the bow is forward of the GPS unit
    static let distanceToBow = 3.5
    // static let distanceToBow = 0.0
    // Overall boat length
    static let boatLength = 10.5
    // static let boatLength = 1.0
    
    static let useMagneticNorth = true

    static let magneticDeclination = 15.31 // Degrees
    // If you measure north MAGNETIC, add this to get the TRUE.
    static func magneticNorthToTrue(degrees: Double) -> Double {
        return Mapping.compassAngle(degrees + magneticDeclination)
    }
    // If you measure north TRUE, subtract to get MAGNETIC
    static func trueNorthToMagnetic(degrees: Double) -> Double {
        return Mapping.compassAngle(degrees - magneticDeclination)
    }
    
    /// Take a TRUE north reading and convert to our local system (either TRUE or MAG)
    static func compassAdjustTrue(trueNorthDegrees: Double) -> Double {
        if useMagneticNorth {
            return trueNorthToMagnetic(degrees: trueNorthDegrees)
        } else {
            return trueNorthDegrees
        }
    }

    /// Take a MAG north reading and convert to our local system (either TRUE or MAG)
    static func compassAdjustMagnetic(magneticNorthDegrees: Double) -> Double {
        if useMagneticNorth {
            return magneticNorthDegrees
        } else {
            return magneticNorthToTrue(degrees: magneticNorthDegrees)
        }
    }

    
    
}
