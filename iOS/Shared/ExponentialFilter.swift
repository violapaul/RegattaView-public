//
//  ExponentialFilter.swift
//  BoatStart
//
//  Created by Paul Viola on 1/2/23.
//

import Foundation

class ExponentialFilter {
    
    var alpha: Double
    var beta0: Double
    var beta: Double
    var maxError: Double
    var currentVal: Double
    
    /// Apply a non-linear exponential filter, where alpha is the decay (higher alpha is longer decay and higher smoothing).
    /// If the error is greater than maxError, then alpha is repeatedly reduced (faster decay).
    init(alpha: Double, maxError: Double, initial: Double = 0.0){
        self.alpha = alpha
        // Use beta because the math is simpler
        self.beta0 = 1 - alpha
        self.beta = self.beta0
        self.maxError = maxError
        self.currentVal = initial
    }

    func update(_ value: Double) -> Double {
        let res = (1.0 - beta) * self.currentVal + beta * value
        if abs(res - value) > self.maxError {
            // High residual error (slew is high) increase beta (track faster)
            beta = min(1.5 * beta, 0.5)
        } else {
            // Residual is low, converge back to beta0
            beta = (beta + beta0) / 2.0
        }
        self.currentVal = res
        return res
    }
}
