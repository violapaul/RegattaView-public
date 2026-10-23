//
//  Alert.swift
//  BoatTimer
//
//  Created by Paul Viola on 12/2/22.
//

import Foundation

import AVFoundation
import UIKit

struct Alert {

    static let feedbackGenerator = UINotificationFeedbackGenerator()
    static let impactGenerator = UIImpactFeedbackGenerator(style: .heavy)

    static var sounds: [String: AVAudioPlayer] = [:]
    
    static func makePlayer(name: String) -> AVAudioPlayer? {
        guard let path = Bundle.main.path(forResource: name, ofType:"mp3") else {
            print("Could not find audio file")
            return nil
        }
        let url = URL(fileURLWithPath: path)
        
        do {
            return try AVAudioPlayer(contentsOf: url)
        } catch let error {
            print("Error!")
            print(error.localizedDescription)
            return nil
        }
    }
    
    
    static func play(name: String, volume: Float, count: Int = 0) {
        var player = sounds[name]
        if player == nil {
            player = makePlayer(name: name)
        }
        guard let player = player else { return }
        sounds[name] = player
        player.volume = volume
        player.numberOfLoops = count
        player.play()
    }
    
    static func success() {
        feedbackGenerator.notificationOccurred(.success)
    }

    static func emphasize(intensity: CGFloat) {
        impactGenerator.impactOccurred(intensity: intensity)
    }

    
    static func buttonPress() {
        success()
        play(name: "button", volume: 0.5)
    }
    
    static func warn1() {
        emphasize(intensity: 1.0)
        play(name: "warn", volume: 1.0)
    }
    
    static func warn2() {
        emphasize(intensity: 0.8)
        play(name: "beep", volume: 1.0)
    }

    static func warn30() {
        emphasize(intensity: 1.0)
        play(name: "beep", volume: 1.0, count: 3)
    }

    static func warn20() {
        emphasize(intensity: 1.0)
        play(name: "beep", volume: 1.0, count: 2)
    }

    static func warn10() {
        emphasize(intensity: 1.0)
        play(name: "beep", volume: 1.0, count: 1)
    }
    
    static func start() {
        emphasize(intensity: 1.0)
        play(name: "start", volume: 1.0)
    }
}
