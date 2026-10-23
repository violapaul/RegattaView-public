//
//  Vector.swift
//  BoatStart
//
//  Created by Paul Viola on 1/30/23.
//

import Foundation

class Vector : Codable {
    var x: Double
    var y: Double
    var z: Double

    var length: Double {
        get {
            return sqrt(x*x + y*y + z*z)
        }
    }

    var lengthSquared: Double {
        get {
            return (x*x + y*y + z*z)
        }
    }
    
    var lat: Double {
        get {
            return self.x
        }
    }
    var lon: Double {
        get {
            return self.y
        }
    }

    init(x: Double, y: Double, z: Double) {
        self.x = x
        self.y = y
        self.z = z
    }

    init() {
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
    }
    
    init(_ x: Double, _ y: Double, _ z: Double) {
        self.x = x
        self.y = y
        self.z = z
    }

    init(_ x: Double, _ y: Double) {
        self.x = x
        self.y = y
        self.z = 0.0
    }
    
    func dot(_ v: Vector) -> Double {
        return (x * v.x) + (y * v.y) + (z * v.z)
    }
   
}

func +(v1: Vector, v2: Vector) -> Vector {
  return Vector(v1.x + v2.x, v1.y + v2.y, v1.z + v2.z)
}

func -(v1: Vector, v2: Vector) -> Vector {
  return Vector(v1.x - v2.x, v1.y - v2.y, v1.z - v2.z)
}

func ==(v1: Vector, v2: Vector) -> Bool {
  return (v1.x == v2.x) && (v1.y == v2.y) && (v1.z == v2.z)
}

func !=(v1: Vector, v2: Vector) -> Bool {
    return !(v1 == v2)
}

func /(v: Vector, scalar: Double) -> Vector {
    return Vector(v.x/scalar, v.y/scalar, v.z/scalar)
}

func *(v: Vector, scalar: Double) -> Vector {
    return Vector(v.x*scalar, v.y*scalar, v.z*scalar)
}

func *(scalar: Double, v: Vector) -> Vector {
    return Vector(v.x*scalar, v.y*scalar, v.z*scalar)
}

