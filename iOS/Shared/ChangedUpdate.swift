//
//  ChangedUpdate.swift
//  BoatStart
//
//  Created by Paul Viola on 11/21/22.
//

import Foundation
import CoreLocation
import SwiftUI

/// Class that provides a did change callback.  The children must call didChange for the callbacks to be executed.
class ChangedUpdate: NSObject {
    
    typealias callback = () -> Void
    private var onChangedCallbacks: [callback] = []
    
    override init() {
        super.init()
    }
    
    /// Register the did change callback
    func addOnChanged(_ cb: @escaping callback) {
        onChangedCallbacks.append(cb)
    }
    
    func didChange() {
        for cb in onChangedCallbacks {
            cb()
        }
    }
}

