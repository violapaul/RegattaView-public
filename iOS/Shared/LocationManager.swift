//
//  LocationManager.swift
//  GPSTest
//
//  Created by Paul Viola on 11/27/21.
//


import Foundation
import CoreLocation
import SwiftUI


/// Wraps and interfaces with iphone locaiton manager.  Allows us to capture changes in GPS location and compass heading.
class LocationManager: ChangedUpdate, CLLocationManagerDelegate {
    
    private let locationManager = CLLocationManager()
    
    // Cached location information, which is updated by the callbacks.
    var locationStatus: CLAuthorizationStatus?
    var lastLocation: CLLocation?
    var lastHeading: CLHeading?

    
    override init() {
        super.init()
        
        locationManager.delegate = self
        locationManager.requestWhenInUseAuthorization()
        
        // Setup GPS info (lat, lon, sog, cog).
        locationManager.desiredAccuracy = kCLLocationAccuracyBest
        locationManager.startUpdatingLocation()
        // Setup heading
        // Heading did not work at first... seemed to require specifying this.
        locationManager.headingOrientation = CLDeviceOrientation.faceUp
        locationManager.headingFilter = 1 // Report any change more than 1 degree.
        locationManager.startUpdatingHeading()
    }
    

    var statusString: String {
        guard let status = locationStatus else {
            return "unknown"
        }
        
        switch status {
        case .notDetermined: return "notDetermined"
        case .authorizedWhenInUse: return "authorizedWhenInUse"
        case .authorizedAlways: return "authorizedAlways"
        case .restricted: return "restricted"
        case .denied: return "denied"
        default: return "unknown"
        }
    }
    
    // Callbacks follow.  These are created by implementing methods in the CLLocationManagerDelegate interface
    
    func locationManager(_ manager: CLLocationManager, didChangeAuthorization status: CLAuthorizationStatus) {
        locationStatus = status
        // print#function, statusString)
    }
    

    // Called when the location is updated.
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let loc = locations.last else { return }
        lastLocation = loc
        didChange()
    }
    
    // Called when heading is updated.
    func locationManager(_ manager: CLLocationManager, didUpdateHeading heading: CLHeading) {
        lastHeading = heading
        didChange()
    }
    
}

