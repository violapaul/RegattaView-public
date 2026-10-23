//
//  Dimmer.swift
//  BoatStart
//
//  Created by Paul Viola on 1/17/23.
//

import Foundation
import SwiftUI

class Dimmer {
    var timer: Timer?
    var delay: Double = 10 * 60.0
    // var dim = 0.3
    var setting = 0
    // Loop through these dim settings
    var dimSettings = [0.3, 0.7, 1.0]
    
    init(delay: Double) {
        self.delay = delay
        tap()
    }

    init(delay: Double, settings: [Double]) {
        self.delay = delay
        self.dimSettings = settings
        tap()
    }
    
    func set(_ val: Double) {
        print("Setting brightness to \(val)")
        UIScreen.main.brightness = CGFloat(val)
    }

    /// When the screen is shifted, then set brightness again.
    func tap() {
        set(self.dimSettings[setting])
        setting = (setting+1) % self.dimSettings.count
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false, block: { _ in  self.set(self.dimSettings[0]) } )
    }
}
