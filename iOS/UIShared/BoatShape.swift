//
//  BoatShape.swift
//  BoatStart
//
//  Created by Paul Viola on 11/20/22.
//

import Foundation
import SwiftUI

/// Create a transform that rotates, scales, and then transforms
func rotScaleTrans(degrees: Double, scale: Double, tx: Double, ty: Double) -> CGAffineTransform {
    let rot = CGAffineTransform(rotationAngle: Angle.degrees(degrees).radians)
    let translate = CGAffineTransform(translationX: tx, y: ty)
    let scale = CGAffineTransform(scaleX: scale, y: scale)
    return rot.concatenating(scale.concatenating(translate))
}

struct BoatShape: Shape {
    let boatShape: [(Double, Double)] = [( 2.10198727e-18, -4.66243153e-01),
                                         ( 1.10514795e-01, -4.66243153e-01),
                                         ( 1.47410654e-01, -1.90822394e-01),
                                         ( 1.54061625e-01,  7.60289751e-03),
                                         ( 1.25198640e-01,  2.02315955e-01),
                                         ( 6.51117286e-02,  3.81620964e-01),
                                         ( 1.99418999e-18,  5.31768883e-01),
                                         ( 1.99418999e-18,  5.31768883e-01),
                                         (-6.51117286e-02,  3.81620964e-01),
                                         (-1.25198640e-01,  2.02315955e-01),
                                         (-1.54061625e-01,  7.60289751e-03),
                                         (-1.47410654e-01, -1.90822394e-01),
                                         (-1.10514795e-01, -4.66243153e-01),
                                         ( 1.88639271e-18, -4.66243153e-01)]
    
    func path(in rect: CGRect) -> Path {
        let s = min(rect.height, rect.width)
        // Scale to fit the rect, and center.
        let trans = rotScaleTrans(degrees: 180, scale: s, tx: rect.midX, ty: rect.midY)
        
        var path = Path()
        
        let (xstart, ystart) = boatShape[0]
        path.move(to: CGPoint(x: xstart, y: ystart))

        for (x, y) in boatShape.dropFirst(1)  {
            path.addLine(to: CGPoint(x: x, y: y))
        }
        path.closeSubpath()
        
        // Need to convert this back to a path at the end
        return path.transform(trans).path(in: rect)
    }
}


struct MidLine: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        
        path.move(to: CGPoint(x: 0, y: rect.midY))
        path.addLine(to: CGPoint(x: rect.width, y: rect.midY))
        path.closeSubpath()
        
        // Need to convert this back to a path at the end
        return path
    }
}

struct BargeLine: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        
        path.move(to: CGPoint(x: rect.midX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
        path.move(to: CGPoint(x: rect.midX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
        path.closeSubpath()
        
        // Need to convert this back to a path at the end
        return path
    }
}
