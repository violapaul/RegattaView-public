//
//  BoatStartApp.swift
//  BoatStart
//
//  Created by Paul Viola on 10/30/22.
//

import SwiftUI

// The hacking below is to enable a tap anywhere to pop up the brightness.
extension UIApplication {
    func addGestureRecognizer() {
        let window = UIApplication
            .shared
            .connectedScenes
            .compactMap { ($0 as? UIWindowScene)?.keyWindow }
            .first
        if window == nil { return }
        let gesture = UITapGestureRecognizer(target: window, action: nil)
        gesture.requiresExclusiveTouchType = false
        gesture.cancelsTouchesInView = false
        gesture.delegate = self
        window!.addGestureRecognizer(gesture)
    }
}

extension UIApplication: UIGestureRecognizerDelegate {
    public func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        print("touch detected")
        return true
    }

    public func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool {
        return true
    }
}



@main
struct BoatStartApp: App {
    var body: some Scene {
        
        let dimmer = Dimmer(delay: 10.0 * 60.0)
        
        WindowGroup {
            ContentView()
                .onAppear(perform: UIApplication.shared.addGestureRecognizer)
                .gesture(
                    TapGesture()
                        .onEnded { _ in
                            dimmer.tap()
                        }
                )
        }
    }
}
