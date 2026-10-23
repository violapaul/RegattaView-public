//
//  Throttler.swift
//  BoatStart
//
//  Created by Paul Viola on 11/26/22.
//

import Foundation

/// Used to throttle message/events.  isReady returns true if the time since the last call to isReady is more than deltaT
class Throttler {
    var tic = Date.now
    var deltaT = 1.0
    
    init(deltaT: Double) {
        self.deltaT = deltaT
    }
    
    func isReady() -> Bool {
        let toc = Date.now
        if toc.timeIntervalSince(tic) > self.deltaT {
            tic = toc
            return true
        } else {
            return false
        }
    }
    
    func force() {
        tic = Date.now
    }
}
