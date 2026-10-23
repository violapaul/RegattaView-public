//
//  Ux.swift
//  BoatStart
//
//  Created by Paul Viola on 4/14/23.
//

import SwiftUI

struct Ux {
    static func pf(_ value: Double?, _ precision: Int = 2, ifMissing: Double = -1.0) -> String {
        let val = value ?? ifMissing
        return String(format: "%.\(precision)F", val)
    }

    static func pf(_ value: Int?, _ precision: Int = 2, ifMissing: Int = -1) -> String {
        let val = value ?? ifMissing
        return String(format: "%.\(precision)d", val)
    }

    static func makeTap(_ message: String) -> some Gesture {
        return  TapGesture(count: 1) .onEnded { print(message) }
    }
    
    static let bigPoint: CGFloat = 180.0

    static let bigFont = Font
        .system(size: Ux.bigPoint)
        .monospacedDigit()

    static let mediumPoint: CGFloat = 90.0

    static let mediumFont = Font
        .system(size: Ux.mediumPoint)
        .monospacedDigit()

    static let defaultPoint: CGFloat = 60.0

    static let defaultFont = Font
        .system(size: Ux.defaultPoint)
        .monospacedDigit()

    
    static let buttonFont = Font
        .system(size: 80)
        .monospaced()
    
    static let tinyFont = Font
        .system(size: 30)
        .monospacedDigit()
    
}
